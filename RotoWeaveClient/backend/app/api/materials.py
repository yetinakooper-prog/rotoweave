from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from ..limits import MAX_MEDIA_UPLOAD_BYTES
from ..material_library import MaterialLibrary
from ..material_photoshop_sheet_v4 import (
    export_material_sheet,
    import_material_sheet,
    material_sheet_path,
)
from ..domain_shadows import resolve_domain_action_shadows, resolve_domain_core_shadow
from ..network import is_loopback_host
from ..schemas import BasicMaterialSettings, ChromaSettings, CurrentModel
from ..size_system import prepare_core_reference
from ..workspace_format import WorkspaceFormatError, resolve_workspace_path
from .context import ApiContext
from .presenters import _public_job


class DomainCharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expectedRevisionId: str | None = None


class DomainCharacterUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expectedRevisionId: str = Field(min_length=1, max_length=160)


class DomainCharacterSettingsUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    expectedRevisionId: str = Field(min_length=1, max_length=160)
    calibration: dict[str, Any] | None = None
    shadow: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None


class BasicMaterialJobRequest(BaseModel):
    expectedRevisionId: str = Field(min_length=1, max_length=160)
    frameIndexes: list[int] = Field(min_length=1, max_length=100_000)
    settings: BasicMaterialSettings

    @field_validator("frameIndexes")
    @classmethod
    def current_frame_indexes(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("frameIndexes 不能包含负数。")
        if value != sorted(set(value)):
            raise ValueError("frameIndexes 必须严格升序且不能重复。")
        return value


class RemoteMaterialSettings(CurrentModel):
    material_type: Literal["character", "effect"]
    chroma: ChromaSettings = Field(default_factory=ChromaSettings)
    ai_assist: bool = True


class RemoteMaterialJobRequest(BaseModel):
    expectedRevisionId: str = Field(min_length=1, max_length=160)
    frameIndexes: list[int] = Field(min_length=1, max_length=100_000)
    quality: Literal["high", "ultra"]
    settings: RemoteMaterialSettings

    @field_validator("frameIndexes")
    @classmethod
    def current_frame_indexes(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("frameIndexes 不能包含负数。")
        if value != sorted(set(value)):
            raise ValueError("frameIndexes 必须严格升序且不能重复。")
        return value


class PhotoshopSheetExportRequest(BaseModel):
    variantId: str | None = Field(default=None, max_length=160)
    frameIndexes: list[int] | None = Field(default=None, max_length=100_000)
    batchSize: int = Field(default=32, ge=1, le=128)


class DomainShadowPreviewRequest(BaseModel):
    frameRefs: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    loop: bool = False
    useCoreReference: bool = False
    shadowStandardY: float | None = None
    shadow: dict[str, Any] | None = None


_PHOTOSHOP_SHEET_NAME = re.compile(
    r"^RotoWeave-PS-([0-9a-f]{32})-part-([0-9]{3})-of-([0-9]{3})\.png$",
    re.IGNORECASE,
)


def _ordered_photoshop_uploads(
    files: list[UploadFile], expected_sheet_id: str | None = None
) -> tuple[str, list[UploadFile]]:
    parsed: list[tuple[int, int, str, UploadFile]] = []
    for file in files:
        match = _PHOTOSHOP_SHEET_NAME.fullmatch(file.filename or "")
        if match is None:
            raise HTTPException(422, "Photoshop 回导文件名无效或已重命名。")
        parsed.append((int(match.group(2)), int(match.group(3)), match.group(1).lower(), file))
    if not parsed:
        raise HTTPException(422, "请选择完整的 Photoshop 拼图批次。")
    sheet_ids = {item[2] for item in parsed}
    totals = {item[1] for item in parsed}
    if len(sheet_ids) != 1 or len(totals) != 1:
        raise HTTPException(422, "Photoshop 回导不能混合不同导出会话。")
    sheet_id = next(iter(sheet_ids))
    total = next(iter(totals))
    if expected_sheet_id and expected_sheet_id.lower() != sheet_id:
        raise HTTPException(422, "Photoshop 回导会话与所选文件名不一致。")
    parts = [item[0] for item in parsed]
    if total < 1 or total != len(parsed) or sorted(parts) != list(range(1, total + 1)):
        raise HTTPException(422, "Photoshop 回导批次缺失、重复或数量不一致。")
    return sheet_id, [item[3] for item in sorted(parsed, key=lambda item: item[0])]


def _library(context: ApiContext) -> MaterialLibrary:
    repository = context.database.session.require_repository()
    return MaterialLibrary(repository, context.settings, Path(context.store.runtime_root))


def _domain_result(context: ApiContext, **values: Any) -> dict[str, Any]:
    domain = context.database.workspace_domain()
    return {**values, "revisionId": domain["revisionId"], "domain": domain}


def register_material_routes(router: APIRouter, context: ApiContext) -> None:
    database = context.database
    store = context.store

    @router.get("/domain")
    def workspace_domain() -> dict[str, Any]:
        return database.workspace_domain()

    @router.post("/domain/characters", status_code=201)
    def create_domain_character(payload: DomainCharacterCreate) -> dict[str, Any]:
        character = database.create_domain_character(
            payload.name,
            expected_revision_id=payload.expectedRevisionId,
        )
        return _domain_result(context, character=character)

    @router.patch("/domain/characters/{character_id}")
    def update_domain_character(
        character_id: str, payload: DomainCharacterUpdate
    ) -> dict[str, Any]:
        character = database.update_domain_character(
            character_id,
            name=payload.name,
            expected_revision_id=payload.expectedRevisionId,
        )
        return _domain_result(context, character=character)

    @router.delete("/domain/characters/{character_id}")
    def delete_domain_character(
        character_id: str,
        expected_revision_id: str = Query(...),
        explicit: bool = Query(False),
    ) -> dict[str, Any]:
        if not explicit:
            raise HTTPException(422, "角色只能由用户显式删除。")
        result = database.delete_domain_character(
            character_id,
            expected_revision_id=expected_revision_id,
        )
        result["reclaimedBytes"] = database.remove_domain_asset_files(
            result["assetPaths"]
        )
        return _domain_result(context, deletion=result)

    @router.post("/domain/characters/{character_id}/reveal")
    def reveal_domain_character_directory(
        character_id: str, request: Request
    ) -> dict[str, Any]:
        host = request.client.host if request.client else None
        if not is_loopback_host(host):
            raise HTTPException(403, "角色目录只能由运行客户端的本机打开。")
        repository = database.session.require_repository()
        character = next(
            (item for item in repository.workspace_domain().get("characters") or [] if item.get("id") == character_id),
            None,
        )
        if character is None:
            raise HTTPException(404, "角色不存在。")
        opener = getattr(os, "startfile", None)
        if opener is None:
            raise HTTPException(409, "角色目录打开功能仅支持 Windows 客户端。")
        target = repository.ensure_workspace_directory(
            repository.root / "characters" / character_id
        )
        opener(str(target))
        return {"opened": True, "characterId": character_id}

    @router.patch("/domain/characters/{character_id}/settings")
    def update_domain_character_settings(character_id: str, payload: DomainCharacterSettingsUpdate) -> dict[str, Any]:
        changes = payload.model_dump(exclude={"expectedRevisionId"}, exclude_none=True)
        character = database.update_domain_character_settings(character_id, changes, expected_revision_id=payload.expectedRevisionId)
        return _domain_result(context, character=character)

    @router.post("/domain/characters/{character_id}/shadow-preview")
    def preview_domain_shadow(
        character_id: str, payload: DomainShadowPreviewRequest
    ) -> dict[str, Any]:
        repository = database.session.require_repository()
        domain = repository.workspace_domain()
        character = next(
            (item for item in domain.get("characters") or [] if item.get("id") == character_id),
            None,
        )
        if character is None:
            raise HTTPException(404, "角色不存在。")
        try:
            if payload.useCoreReference:
                return {
                    "frames": [
                        item
                        for item in [resolve_domain_core_shadow(
                            repository.root,
                            character,
                            shadow_standard_y=payload.shadowStandardY,
                            shadow_override=payload.shadow,
                        )]
                        if item is not None
                    ]
                }
            if not payload.frameRefs:
                raise HTTPException(422, "动作阴影预览至少需要一个帧引用。")
            return {
                "frames": resolve_domain_action_shadows(
                    repository.root,
                    domain,
                    character,
                    payload.frameRefs,
                    loop=payload.loop,
                    shadow_standard_y=payload.shadowStandardY,
                    shadow_override=payload.shadow,
                )
            }
        except WorkspaceFormatError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/domain/characters/{character_id}/core-reference")
    def upload_domain_core_reference(character_id: str, file: UploadFile = File(...), expected_revision_id: str = Form(...)) -> dict[str, Any]:
        if not (file.filename or "").lower().endswith(".png"):
            raise HTTPException(422, "核心角色图必须是透明 PNG。")
        incoming, _, _, incoming_created = store.put_stream(file.file, file.filename or "core.png", MAX_MEDIA_UPLOAD_BYTES)
        try:
            temporary_root = Path(store.runtime_root) / "temp"
            temporary_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="core-reference-", dir=temporary_root) as directory:
                prepared = Path(directory) / "core.png"
                try:
                    prepare_core_reference(incoming, prepared)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                character = database.set_domain_core_reference(character_id, prepared, expected_revision_id=expected_revision_id)
            return _domain_result(context, character=character)
        finally:
            if incoming_created:
                incoming.unlink(missing_ok=True)

    @router.get("/domain/characters/{character_id}/core-reference")
    def get_domain_core_reference(character_id: str) -> FileResponse:
        character = next((item for item in database.workspace_domain()["characters"] if item["id"] == character_id), None)
        asset = ((character or {}).get("calibration") or {}).get("coreReference")
        if not isinstance(asset, dict):
            raise HTTPException(404, "核心角色图不存在。")
        return FileResponse(
            resolve_workspace_path(database.session.require_repository().root, asset["path"]),
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.delete("/domain/characters/{character_id}/core-reference")
    def delete_domain_core_reference(character_id: str, expected_revision_id: str = Query(...)) -> dict[str, Any]:
        character = database.delete_domain_core_reference(character_id, expected_revision_id=expected_revision_id)
        return _domain_result(context, character=character)

    def import_one(
        character_id: str,
        file: UploadFile,
        display_name: str,
        target_fps: float | None,
        expected_revision_id: str | None,
    ) -> dict[str, Any]:
        incoming, _, _, _ = store.put_stream(
            file.file,
            file.filename or "video.mp4",
            MAX_MEDIA_UPLOAD_BYTES,
        )
        result = _library(context).import_video(
            character_id,
            incoming,
            display_name or Path(file.filename or "video").stem,
            target_fps=target_fps,
            expected_revision_id=expected_revision_id,
        )
        return _domain_result(context, **result)

    @router.post("/domain/characters/{character_id}/materials/import", status_code=201)
    def import_material(
        character_id: str,
        file: UploadFile = File(...),
        display_name: str = Form(""),
        target_fps: float | None = Form(None),
        expected_revision_id: str | None = Form(None),
    ) -> dict[str, Any]:
        return import_one(
            character_id,
            file,
            display_name,
            target_fps,
            expected_revision_id,
        )

    @router.post("/domain/characters/{character_id}/materials/sync")
    def sync_materials(
        character_id: str,
        files: list[UploadFile] = File(...),
        target_fps: float | None = Form(None),
        expected_revision_id: str | None = Form(None),
    ) -> dict[str, Any]:
        imported: list[dict[str, Any]] = []
        revision = expected_revision_id
        for file in files:
            result = import_one(
                character_id,
                file,
                Path(file.filename or "video").stem,
                target_fps,
                revision,
            )
            imported.append(
                {
                    "source": result["source"],
                    "duplicate": result["duplicate"],
                    "report": result["report"],
                }
            )
            revision = result["revisionId"]
        return _domain_result(context, imported=imported)

    @router.post("/domain/characters/{character_id}/materials/import-jobs", status_code=202)
    def create_material_import_job(
        character_id: str,
        files: list[UploadFile] = File(...),
        target_fps: float | None = Form(None),
        expected_revision_id: str = Form(...),
    ) -> dict[str, Any]:
        staged: list[dict[str, Any]] = []
        for file in files:
            path, digest, size, _ = store.put_stream(
                file.file,
                file.filename or "video.mp4",
                MAX_MEDIA_UPLOAD_BYTES,
            )
            staged.append({
                "path": str(path),
                "name": file.filename or "video.mp4",
                "displayName": Path(file.filename or "video").stem,
                "sha256": digest,
                "bytes": size,
            })
        try:
            job = context.jobs.create_material_import(
                character_id,
                staged,
                target_fps=target_fps,
                expected_revision_id=expected_revision_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "角色不存在。") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return _public_job(job)

    @router.get("/material-sources/{source_id}/video")
    def material_video(source_id: str) -> FileResponse:
        source = database.get_material_source(source_id)
        if source is None:
            raise HTTPException(404, "素材源不存在。")
        video = source["video"]
        target = resolve_workspace_path(database.root, video["path"])
        if not target.is_file():
            raise HTTPException(409, "源视频缺失或已损坏。")
        return FileResponse(target, filename=target.name)

    @router.get("/material-sources/{source_id}/frames/{frame_index}/thumbnail")
    def material_thumbnail(source_id: str, frame_index: int) -> FileResponse:
        try:
            target = _library(context).thumbnail_path(source_id, frame_index)
        except WorkspaceFormatError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(target, media_type="image/jpeg")

    @router.get("/material-sources/{source_id}/frames/{frame_index}")
    def material_source_frame(source_id: str, frame_index: int) -> FileResponse:
        source = database.get_material_source(source_id)
        frames = (source or {}).get("frames") or []
        if frame_index < 0 or frame_index >= len(frames):
            raise HTTPException(404, "源帧不存在。")
        target = resolve_workspace_path(database.root, frames[frame_index]["path"])
        if not target.is_file():
            raise HTTPException(409, "源帧缺失或已损坏。")
        return FileResponse(target, media_type="image/png")

    @router.get("/material-variants/{variant_id}/frames/{frame_index}")
    def material_variant_frame(
        variant_id: str,
        frame_index: int,
        layer: Literal["rgba", "emission"] = Query("rgba"),
    ) -> FileResponse:
        variant = database.get_material_variant(variant_id)
        frames = (variant or {}).get("frames") or []
        if frame_index < 0 or frame_index >= len(frames):
            raise HTTPException(404, "处理帧不存在。")
        record = frames[frame_index]
        asset = record if layer == "rgba" else record.get("emission")
        if not isinstance(asset, dict):
            raise HTTPException(404, "处理帧没有该图层。")
        target = resolve_workspace_path(database.root, str(asset.get("path") or ""))
        if not target.is_file():
            raise HTTPException(409, "处理帧缺失或已损坏。")
        return FileResponse(target, media_type="image/png")

    @router.post("/material-sources/{source_id}/variants", status_code=201)
    def publish_variant(
        source_id: str,
        files: list[UploadFile] = File(...),
        kind: Literal["basic", "high", "ultra", "photoshop"] = Form(...),
        settings_json: str = Form("{}"),
        expected_revision_id: str | None = Form(None),
    ) -> dict[str, Any]:
        try:
            settings = json.loads(settings_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "处理设置不是有效 JSON。") from exc
        if not isinstance(settings, dict):
            raise HTTPException(422, "处理设置必须是 JSON 对象。")
        incoming: list[str] = []
        for file in files:
            path, _, _, _ = store.put_stream(
                file.file,
                file.filename or "frame.png",
                MAX_MEDIA_UPLOAD_BYTES,
            )
            incoming.append(str(path))
        variant = database.publish_material_variant(
            source_id,
            kind,
            incoming,
            settings,
            expected_revision_id=expected_revision_id,
        )
        return _domain_result(context, variant=variant)

    @router.post("/material-sources/{source_id}/basic-jobs", status_code=202)
    def create_basic_job(
        source_id: str,
        payload: BasicMaterialJobRequest,
    ) -> dict[str, Any]:
        try:
            job = context.jobs.create_material_basic(
                source_id,
                payload.settings.model_dump(mode="json"),
                expected_revision_id=payload.expectedRevisionId,
                frame_indexes=payload.frameIndexes,
            )
        except KeyError as exc:
            raise HTTPException(404, "素材源不存在。") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _public_job(job)

    @router.post("/material-sources/{source_id}/remote-jobs", status_code=202)
    def create_remote_job(
        source_id: str,
        payload: RemoteMaterialJobRequest,
    ) -> dict[str, Any]:
        try:
            job = context.jobs.create_material_remote(
                source_id,
                payload.quality,
                payload.settings.model_dump(mode="json"),
                expected_revision_id=payload.expectedRevisionId,
                frame_indexes=payload.frameIndexes,
            )
        except KeyError as exc:
            raise HTTPException(404, "素材源不存在。") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _public_job(job)

    @router.post("/material-sources/{source_id}/photoshop-sheet/export")
    def export_photoshop_sheet(
        source_id: str,
        payload: PhotoshopSheetExportRequest = Body(default_factory=PhotoshopSheetExportRequest),
    ) -> dict[str, Any]:
        try:
            result = export_material_sheet(
                database.session.require_repository(),
                Path(store.runtime_root),
                source_id,
                variant_id=payload.variantId,
                frame_indexes=payload.frameIndexes,
                batch_size=payload.batchSize,
            )
        except KeyError as exc:
            raise HTTPException(404, "素材源不存在。") from exc
        except WorkspaceFormatError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            **result,
            "sheets": [
                {
                    **batch,
                    "downloadUrl": (
                        f"/api/v4/material-sources/{source_id}/photoshop-sheet/download"
                        f"?sheet_id={result['sheetId']}&batch_index={batch['batchIndex']}"
                    ),
                }
                for batch in result["batches"]
            ],
        }

    @router.get("/material-sources/{source_id}/photoshop-sheet/download")
    def download_photoshop_sheet(
        source_id: str,
        sheet_id: str = Query(...),
        batch_index: int = Query(0, ge=0, le=999),
    ) -> FileResponse:
        try:
            target = material_sheet_path(Path(store.runtime_root), source_id, sheet_id, batch_index)
        except (FileNotFoundError, WorkspaceFormatError) as exc:
            raise HTTPException(404, "Photoshop 拼图不存在或已过期。") from exc
        return FileResponse(
            target,
            media_type="image/png",
            filename=(
                f"RotoWeave-PS-{sheet_id}-part-{batch_index + 1:03d}-of-"
                f"{len(json.loads((target.parent / 'manifest.json').read_text(encoding='utf-8')).get('batches') or []):03d}.png"
            ),
        )

    @router.post("/material-sources/{source_id}/photoshop-sheet/import", status_code=201)
    def import_photoshop_sheet(
        source_id: str,
        files: list[UploadFile] = File(...),
        sheet_id: str | None = Form(None),
        expected_revision_id: str = Form(...),
    ) -> dict[str, Any]:
        resolved_sheet_id, ordered_files = _ordered_photoshop_uploads(files, sheet_id)
        incoming: list[Path] = []
        for file in ordered_files:
            path, _, _, _ = store.put_stream(
                file.file,
                file.filename or "photoshop-sheet.png",
                MAX_MEDIA_UPLOAD_BYTES,
            )
            incoming.append(path)
        try:
            variant = import_material_sheet(
                database.session.require_repository(),
                Path(store.runtime_root),
                source_id,
                resolved_sheet_id,
                incoming,
                expected_revision_id=expected_revision_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "素材源不存在。") from exc
        except WorkspaceFormatError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _domain_result(context, variant=variant)

    @router.delete("/material-variants/{variant_id}")
    def cleanup_variant(
        variant_id: str,
        expected_revision_id: str = Query(...),
        explicit: bool = Query(False),
    ) -> dict[str, Any]:
        result = database.cleanup_material_variant(
            variant_id,
            explicit=explicit,
            expected_revision_id=expected_revision_id,
        )
        result["reclaimedBytes"] = database.remove_domain_asset_files(
            result["assetPaths"]
        )
        return _domain_result(context, cleanup=result)

    @router.delete("/material-sources/{source_id}")
    def delete_material_source(
        source_id: str,
        expected_revision_id: str = Query(...),
        explicit: bool = Query(False),
    ) -> dict[str, Any]:
        result = database.delete_material_source(
            source_id,
            explicit=explicit,
            expected_revision_id=expected_revision_id,
        )
        result["reclaimedBytes"] = database.remove_domain_asset_files(
            result["assetPaths"]
        )
        _library(context).remove_thumbnail_cache(source_id)
        return _domain_result(context, deletion=result)
