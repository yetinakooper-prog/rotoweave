from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .remote_matting_client import canonical_json_bytes
from .workspace_format import WorkspaceFormatError, resolve_workspace_path
from .workspace_repository import WorkspaceRepository

MAX_SHEET_PIXELS = 268_435_456


def _sheet_root(runtime_root: Path, source_id: str, sheet_id: str) -> Path:
    if not sheet_id.isalnum() or len(sheet_id) > 64:
        raise WorkspaceFormatError("Photoshop 拼图标识无效。")
    return runtime_root.resolve() / "material-photoshop" / source_id / sheet_id


def _material_projection_records(
    repository: WorkspaceRepository,
    source: dict[str, Any],
    through_variant_id: str | None,
) -> list[dict[str, Any]]:
    source_frames = source.get("frames") or []
    if through_variant_id is None:
        return list(source_frames)

    variant_ids = [str(item) for item in source.get("variantIds") or []]
    if through_variant_id not in variant_ids:
        raise WorkspaceFormatError("Photoshop 底图版本不属于当前素材。")
    source_frame_ids = [str(frame.get("id") or "") for frame in source_frames]
    source_frame_id_set = set(source_frame_ids)
    records_by_source_frame_id = {
        source_frame_id: frame
        for source_frame_id, frame in zip(source_frame_ids, source_frames, strict=True)
    }

    for variant_id in variant_ids[: variant_ids.index(through_variant_id) + 1]:
        variant = repository.get_material_variant(variant_id)
        if variant is None or variant.get("sourceId") != source.get("id"):
            raise WorkspaceFormatError("Photoshop 底图版本链已损坏。")
        variant_frames = variant.get("frames") or []
        mapped_ids = [str(frame.get("sourceFrameId") or "") for frame in variant_frames]
        if (
            len(mapped_ids) != len(set(mapped_ids))
            or any(source_frame_id not in source_frame_id_set for source_frame_id in mapped_ids)
        ):
            raise WorkspaceFormatError("Photoshop 底图版本帧映射无效。")
        for source_frame_id, frame in zip(mapped_ids, variant_frames, strict=True):
            records_by_source_frame_id[source_frame_id] = frame

    return [records_by_source_frame_id[source_frame_id] for source_frame_id in source_frame_ids]


def export_material_sheet(
    repository: WorkspaceRepository,
    runtime_root: Path,
    source_id: str,
    *,
    variant_id: str | None = None,
    frame_indexes: list[int] | None = None,
    batch_size: int = 32,
) -> dict[str, Any]:
    source = repository.get_material_source(source_id)
    if source is None:
        raise KeyError(source_id)
    records = _material_projection_records(repository, source, variant_id)
    if not records:
        raise WorkspaceFormatError("素材没有可导出的帧。")
    if batch_size < 1 or batch_size > 128:
        raise WorkspaceFormatError("Photoshop 每批帧数必须在 1–128 之间。")
    selected = list(range(len(records))) if not frame_indexes else list(frame_indexes)
    if len(selected) != len(set(selected)) or any(index < 0 or index >= len(records) for index in selected):
        raise WorkspaceFormatError("Photoshop 选择帧包含重复或越界序号。")

    sheet_id = uuid.uuid4().hex
    root = _sheet_root(runtime_root, source_id, sheet_id)
    root.mkdir(parents=True, exist_ok=False)
    source_frames = source.get("frames") or []
    batches: list[dict[str, Any]] = []
    flat_mapping: list[dict[str, Any]] = []
    try:
        for batch_index, start in enumerate(range(0, len(selected), batch_size)):
            ordinals = selected[start : start + batch_size]
            images: list[Image.Image] = []
            try:
                for ordinal in ordinals:
                    path = resolve_workspace_path(repository.root, str(records[ordinal].get("path") or ""))
                    images.append(Image.open(path).convert("RGBA"))
                cell_width = max(image.width for image in images)
                cell_height = max(image.height for image in images)
                columns = min(8, max(1, math.ceil(math.sqrt(len(images)))))
                rows = math.ceil(len(images) / columns)
                width, height = columns * cell_width, rows * cell_height
                if width * height > MAX_SHEET_PIXELS:
                    raise WorkspaceFormatError("Photoshop 拼图尺寸超过安全上限，请降低每批帧数或分辨率。")
                sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                mapping: list[dict[str, Any]] = []
                for position, (ordinal, image) in enumerate(zip(ordinals, images, strict=True)):
                    x = (position % columns) * cell_width
                    y = (position // columns) * cell_height
                    sheet.alpha_composite(image, (x, y))
                    item = {
                        "ordinal": ordinal,
                        "sourceFrameId": str(source_frames[ordinal]["id"]),
                        "x": x,
                        "y": y,
                        "width": image.width,
                        "height": image.height,
                    }
                    mapping.append(item)
                    flat_mapping.append(item)
                filename = f"sheet-{batch_index:03d}.png"
                sheet.save(root / filename, format="PNG", optimize=True)
                sheet.close()
                batches.append({
                    "batchIndex": batch_index,
                    "filename": filename,
                    "width": width,
                    "height": height,
                    "frameCount": len(mapping),
                    "mapping": mapping,
                })
            finally:
                for image in images:
                    image.close()
        mapping_sha256 = hashlib.sha256(canonical_json_bytes(flat_mapping)).hexdigest()
        manifest = {
            "schemaVersion": 2,
            "sheetId": sheet_id,
            "sourceId": source_id,
            "sourceSha256": str((source.get("video") or {}).get("sha256") or ""),
            "baseVariantId": variant_id,
            "sourceFrameCount": len(records),
            "selectedFrameCount": len(flat_mapping),
            "batchSize": batch_size,
            "batchCount": len(batches),
            "mappingSha256": mapping_sha256,
            "mapping": flat_mapping,
            "batches": batches,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return manifest
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def material_sheet_path(runtime_root: Path, source_id: str, sheet_id: str, batch_index: int = 0) -> Path:
    if batch_index < 0 or batch_index > 999:
        raise WorkspaceFormatError("Photoshop 拼图批次序号无效。")
    target = _sheet_root(runtime_root, source_id, sheet_id) / f"sheet-{batch_index:03d}.png"
    if not target.is_file():
        raise FileNotFoundError(sheet_id)
    return target


def import_material_sheet(
    repository: WorkspaceRepository,
    runtime_root: Path,
    source_id: str,
    sheet_id: str,
    image_paths: list[Path],
    *,
    expected_revision_id: str,
) -> dict[str, Any]:
    root = _sheet_root(runtime_root, source_id, sheet_id)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise WorkspaceFormatError("Photoshop 拼图会话不存在或已过期。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = repository.get_material_source(source_id)
    if source is None:
        raise KeyError(source_id)
    if manifest.get("sourceId") != source_id or manifest.get("sourceSha256") != (source.get("video") or {}).get("sha256"):
        raise WorkspaceFormatError("Photoshop 拼图与当前素材身份不匹配。")
    mapping = manifest.get("mapping") or []
    batches = manifest.get("batches") or []
    if hashlib.sha256(canonical_json_bytes(mapping)).hexdigest() != manifest.get("mappingSha256"):
        raise WorkspaceFormatError("Photoshop 拼图帧映射已损坏。")
    source_frames = source.get("frames") or []
    if len(batches) != len(image_paths):
        raise WorkspaceFormatError("Photoshop 回导批次数量与导出清单不一致。")
    if [item.get("sourceFrameId") for item in mapping] != [source_frames[int(item["ordinal"])].get("id") for item in mapping]:
        raise WorkspaceFormatError("Photoshop 拼图帧映射与当前素材不一致。")

    base_variant_id = manifest.get("baseVariantId")
    base_records = _material_projection_records(repository, source, base_variant_id)
    frame_paths = [str(resolve_workspace_path(repository.root, str(record.get("path") or ""))) for record in base_records]
    staging = root / f"import-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True)
        for batch, image_path in zip(batches, image_paths, strict=True):
            try:
                with Image.open(image_path) as opened:
                    image = opened.convert("RGBA")
            except (UnidentifiedImageError, OSError) as exc:
                raise WorkspaceFormatError("Photoshop 回导文件不是有效 PNG。") from exc
            try:
                if image.size != (int(batch["width"]), int(batch["height"])):
                    raise WorkspaceFormatError("Photoshop 回导尺寸与导出拼图不一致。")
                for item in batch.get("mapping") or []:
                    box = (
                        int(item["x"]), int(item["y"]),
                        int(item["x"]) + int(item["width"]),
                        int(item["y"]) + int(item["height"]),
                    )
                    ordinal = int(item["ordinal"])
                    target = staging / f"{ordinal:06d}.png"
                    image.crop(box).save(target, format="PNG", optimize=True)
                    frame_paths[ordinal] = str(target)
            finally:
                image.close()
        return repository.publish_material_variant(
            source_id,
            "photoshop",
            frame_paths,
            {
                "sheetId": sheet_id,
                "mappingSha256": manifest["mappingSha256"],
                "baseVariantId": base_variant_id,
                "batchSize": manifest["batchSize"],
                "batchCount": manifest["batchCount"],
                "selectedFrameCount": manifest["selectedFrameCount"],
            },
            expected_revision_id=expected_revision_id,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
