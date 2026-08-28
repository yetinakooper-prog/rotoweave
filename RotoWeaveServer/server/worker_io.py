from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2
import numpy as np

from contracts.integrity import sha256_file


SUPPORTED_ROUTES = {"chroma_character", "emissive_vfx"}
_CANDIDATE_ASSET_FIELDS = {
    "mattePath", "emissionPath", "confidencePath", "uncertaintyPath",
    "deliveryBasePath", "deliveryEmissionPath", "compatibilityRgbaPath",
    "conservativeMattePath", "conservativeDeliveryBasePath",
    "conservativeCompatibilityRgbaPath",
}


class WorkerIoError(RuntimeError):
    pass


def _is_reparse_point(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_plain_tree(root: Path) -> None:
    if _is_reparse_point(root):
        raise WorkerIoError("Worker candidate root cannot be a reparse point.")
    for path in root.rglob("*"):
        if _is_reparse_point(path):
            raise WorkerIoError(f"Worker candidate contains a reparse point: {path.name}.")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def encoded_to_linear(rgb: np.ndarray) -> np.ndarray:
    value = np.clip(np.asarray(rgb, dtype=np.float32), 0.0, 1.0)
    return np.where(
        value <= 0.04045,
        value / 12.92,
        np.power((value + 0.055) / 1.055, 2.4),
    ).astype(np.float32)


def bgr_u8_to_linear_rgb(image: np.ndarray, transfer: str = "srgb") -> np.ndarray:
    if image.ndim != 3 or image.shape[2] < 3 or image.dtype != np.uint8:
        raise WorkerIoError("Expected an 8-bit BGR source image.")
    if transfer != "srgb":
        raise WorkerIoError(f"Unsupported encoded transfer: {transfer}.")
    return encoded_to_linear(image[:, :, :3][:, :, ::-1].astype(np.float32) / 255.0)


def write_linear_exr(path: Path, rgb: np.ndarray) -> None:
    linear_rgb = np.asarray(rgb, dtype=np.float32)
    if linear_rgb.ndim != 3 or linear_rgb.shape[2] != 3 or not np.isfinite(linear_rgb).all():
        raise WorkerIoError("Linear EXR RGB must be finite HxWx3 data.")
    params = [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF]
    if hasattr(cv2, "IMWRITE_EXR_COMPRESSION") and hasattr(cv2, "IMWRITE_EXR_COMPRESSION_ZIP"):
        params.extend([cv2.IMWRITE_EXR_COMPRESSION, cv2.IMWRITE_EXR_COMPRESSION_ZIP])
    ok, encoded = cv2.imencode(".exr", np.ascontiguousarray(linear_rgb[:, :, ::-1]), params)
    if not ok:
        raise WorkerIoError("OpenCV failed to encode linear EXR input.")
    _atomic_write(path, encoded.tobytes())


def stage_linear_inputs(
    generation_root: Path,
    frames: list[dict[str, Any]],
    *,
    route: str,
    constraints_hash: str,
    source_sha256: str,
) -> Path:
    if route not in SUPPORTED_ROUTES:
        raise WorkerIoError(f"Unsupported worker route: {route}.")
    generation_root.mkdir(parents=True, exist_ok=False)
    input_root = generation_root / "inputs"
    input_root.mkdir()
    records: list[dict[str, Any]] = []
    for ordinal, frame in enumerate(frames):
        frame_id = str(frame.get("id") or "")
        source = Path(str(frame.get("linear_source_path") or "")).resolve(strict=False)
        if not frame_id or source.suffix.lower() != ".exr" or _is_reparse_point(source) or not source.is_file():
            raise WorkerIoError(f"Linear source authority is unavailable for frame {frame_id or ordinal}.")
        staged = input_root / f"{ordinal:06d}.exr"
        try:
            staged.hardlink_to(source)
        except OSError:
            shutil.copy2(source, staged)
        records.append({
            "frameId": frame_id,
            "frameIndex": int(frame.get("frame_index") or ordinal),
            "sourceExr": str(staged),
            "sourceSha256": sha256_file(staged),
            "width": int(frame.get("width") or 0),
            "height": int(frame.get("height") or 0),
            "timeUs": int(frame.get("time_us") or 0),
            "sourceTimelineOrdinal": int(frame.get("source_timeline_ordinal") or ordinal),
        })
    target = generation_root / "worker-input.manifest.json"
    _atomic_write(target, json.dumps({
        "schemaVersion": 1,
        "route": route,
        "sourceSha256": source_sha256,
        "constraintsHash": constraints_hash,
        "cleanPlate": {"mode": "auto"},
        "frames": records,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return target


def import_worker_candidate(
    worker_result: dict[str, Any],
    *,
    exchange_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    source = exchange_root.resolve(strict=True)
    target = workspace_root.resolve(strict=False)
    if not source.is_dir() or _is_reparse_point(source):
        raise WorkerIoError("Worker candidate exchange is unavailable.")
    _assert_plain_tree(source)
    if target.exists() or _is_reparse_point(target):
        raise WorkerIoError("Worker candidate generation already exists.")
    remapped = json.loads(json.dumps(worker_result, ensure_ascii=False))
    frames = remapped.get("frames")
    if not isinstance(frames, list):
        raise WorkerIoError("Worker returned no candidate frame list.")
    for frame in frames:
        if not isinstance(frame, dict):
            raise WorkerIoError("Worker candidate frame is invalid.")
        for field in _CANDIDATE_ASSET_FIELDS:
            raw = frame.get(field)
            if not raw:
                continue
            candidate = Path(str(raw)).resolve(strict=False)
            try:
                relative = candidate.relative_to(source)
            except ValueError as exc:
                raise WorkerIoError(f"Worker candidate asset escapes its exchange: {candidate.name}.") from exc
            if _is_reparse_point(candidate) or not candidate.is_file():
                raise WorkerIoError(f"Worker candidate asset is unavailable: {candidate.name}.")
            frame[field] = str(target / relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.importing")
    if temporary.exists() or _is_reparse_point(temporary):
        raise WorkerIoError("A stale candidate import is blocking publication.")
    try:
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        _assert_plain_tree(temporary)
        os.replace(temporary, target)
    except BaseException:
        if temporary.is_dir() and not _is_reparse_point(temporary):
            shutil.rmtree(temporary)
        raise
    return remapped
