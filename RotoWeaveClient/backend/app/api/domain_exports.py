from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..domain_character_exporter import estimate_domain_character, export_domain_character, repair_domain_atlas_page
from ..workspace_format import WorkspaceFormatError, resolve_workspace_path
from .context import ApiContext


class DomainCharacterExportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    expectedRevisionId: str = Field(min_length=1, max_length=160)
    atlasMaxSize: int = Field(default=4096, ge=64, le=8192)


def register_domain_export_routes(router: APIRouter, context: ApiContext) -> None:
    @router.get("/domain/characters/{character_id}/export/estimate")
    def estimate_character_export(character_id: str, expected_revision_id: str, atlas_max_size: int | None = None) -> dict:
        return estimate_domain_character(context.database.session.require_repository(), character_id, expected_revision_id=expected_revision_id, atlas_max_size=atlas_max_size)

    @router.post("/domain/characters/{character_id}/export")
    def create_domain_character_export(
        character_id: str, payload: DomainCharacterExportRequest
    ) -> dict:
        repository = context.database.session.require_repository()
        try:
            return export_domain_character(
                repository,
                character_id,
                expected_revision_id=payload.expectedRevisionId,
                atlas_max_size=payload.atlasMaxSize,
            )
        except WorkspaceFormatError as exc:
            if str(exc) == "角色不存在。":
                raise HTTPException(404, str(exc)) from exc
            raise

    @router.get("/domain/characters/{character_id}/export/download")
    def download_domain_character_export(character_id: str) -> FileResponse:
        repository = context.database.session.require_repository()
        domain = repository.workspace_domain()
        character = next(
            (item for item in domain["characters"] if item["id"] == character_id),
            None,
        )
        if character is None:
            raise HTTPException(404, "角色不存在。")
        asset = (character.get("exportState") or {}).get("currentAtlas")
        if not isinstance(asset, dict):
            raise HTTPException(404, "角色尚未导出。")
        target = resolve_workspace_path(repository.root, str(asset.get("path") or ""))
        if not target.is_file():
            raise HTTPException(409, "当前角色包缺失，请重新导出。")
        return FileResponse(
            target,
            media_type="application/octet-stream",
            filename=f"{character['name']}.rotoweave",
        )

    @router.get("/domain/characters/{character_id}/export/pages/{page_index}")
    def preview_domain_atlas_page(character_id: str, page_index: int):
        import io, zipfile
        from fastapi.responses import Response
        repository = context.database.session.require_repository(); domain = repository.workspace_domain()
        character = next((item for item in domain["characters"] if item["id"] == character_id), None)
        asset = ((character or {}).get("exportState") or {}).get("currentAtlas")
        if not isinstance(asset, dict): raise HTTPException(404, "角色尚未导出。")
        archive = resolve_workspace_path(repository.root, asset["path"]); name = f"atlases/base/{page_index:02d}.png"
        with zipfile.ZipFile(archive, "r") as package:
            try: data = package.read(name)
            except KeyError as exc: raise HTTPException(404, "图集页面不存在。") from exc
        return Response(content=data, media_type="image/png")

    @router.post("/domain/characters/{character_id}/export/pages/{page_index}/repair")
    def repair_domain_atlas(character_id: str, page_index: int, file: UploadFile = File(...), expected_revision_id: str = Form(...)) -> dict:
        import tempfile
        if not (file.filename or "").lower().endswith(".png"): raise HTTPException(422, "修复图必须是 PNG。")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            while chunk := file.file.read(1024 * 1024): handle.write(chunk)
            temporary = __import__("pathlib").Path(handle.name)
        try:
            result = repair_domain_atlas_page(context.database.session.require_repository(), character_id, page_index, temporary, expected_revision_id=expected_revision_id)
            result["revisionId"] = context.database.workspace_domain()["revisionId"]
            return result
        finally: temporary.unlink(missing_ok=True)
