from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .birefnet import infer_masks, preflight_birefnet
from .config import Settings
from .images import read_image, write_image
from .processing import chroma_rgba, prepare_frame_evidence, solve_frame_from_evidence
from .schemas import BasicMaterialSettings
from .workspace_format import WorkspaceFormatError, resolve_workspace_path
from .workspace_repository import WorkspaceRepository


BASIC_ALGORITHM_VERSION = "basic-material-v1"
ProgressCallback = Callable[[str, float, str | None], None]
ControlCallback = Callable[[], None]


def _is_oom(error: BaseException) -> bool:
    lowered = str(error).casefold()
    return any(
        token in lowered
        for token in (
            "out of memory",
            "cuda_error_out_of_memory",
            "cuda out of memory",
            "gpu_out_of_memory",
            "显存不足",
        )
    )


def _effect_rgba(image: np.ndarray) -> np.ndarray:
    if image.ndim != 3 or image.shape[2] < 3:
        raise RuntimeError("特效源帧不是有效 RGB 图像。")
    bgr = image[:, :, :3]
    alpha = np.max(bgr, axis=2).astype(np.uint8)
    return np.dstack((bgr, alpha))


def process_basic_material(
    repository: WorkspaceRepository,
    source_id: str,
    output_root: Path,
    raw_settings: dict[str, Any],
    runtime_settings: Settings,
    report: ProgressCallback,
    check_control: ControlCallback,
    *,
    expected_revision_id: str,
    frame_indexes: list[int],
) -> dict[str, Any]:
    settings = BasicMaterialSettings.model_validate(raw_settings)
    source = repository.get_material_source(source_id)
    if source is None:
        raise WorkspaceFormatError("Basic 任务的素材源不存在。")
    source_frames = source.get("frames") or []
    if not source_frames:
        raise WorkspaceFormatError("Basic 任务的素材源没有可处理帧。")
    selected_indexes = list(frame_indexes)
    if not selected_indexes:
        raise WorkspaceFormatError("Basic 任务至少需要一个源帧。")
    if selected_indexes != sorted(set(selected_indexes)) or any(
        index < 0 or index >= len(source_frames) for index in selected_indexes
    ):
        raise WorkspaceFormatError("Basic 任务的源帧选择无效。")
    frames = [source_frames[index] for index in selected_indexes]
    output_root = output_root.resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    warnings: list[str] = []
    model: dict[str, Any] = {
        "id": "chroma-basic",
        "provider": "opencv-cpu",
        "algorithmVersion": BASIC_ALGORITHM_VERSION,
    }
    try:
        source_paths = [
            resolve_workspace_path(repository.root, str(frame["path"]))
            for frame in frames
        ]
        images: list[np.ndarray] = []
        selected_for_ai: list[tuple[int, Path]] = []
        chroma_options = settings.chroma.model_dump(mode="json")
        for batch_index, (source_index, source_path) in enumerate(
            zip(selected_indexes, source_paths, strict=True)
        ):
            check_control()
            image = read_image(source_path, cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError(f"Basic 源帧 {index} 无法读取。")
            images.append(image)
            if settings.material_type == "character" and settings.ai_assist:
                scale = min(1.0, 640.0 / max(image.shape[:2]))
                probe = (
                    cv2.resize(
                        image,
                        (
                            max(1, round(image.shape[1] * scale)),
                            max(1, round(image.shape[0] * scale)),
                        ),
                        interpolation=cv2.INTER_AREA,
                    )
                    if scale < 0.999
                    else image
                )
                _, _, qc = chroma_rgba(probe, chroma_options)
                if (
                    qc.get("empty_mask")
                    or qc.get("fragmented_mask")
                    or qc.get("screen_residue")
                    or qc.get("green_residue")
                    or float(qc.get("uncertain_ratio") or 0) > 0.25
                    or float(qc.get("color_conflict_ratio") or 0) > 0.03
                ):
                    selected_for_ai.append((batch_index, source_path))
            report("basic-preflight", (batch_index + 1) / len(frames) * 0.15, None)

        ai_paths: dict[int, Path] = {}
        if selected_for_ai:
            report(
                "basic-ai",
                0.16,
                f"Basic 将对 {len(selected_for_ai)} 个风险帧运行本地 BiRefNet。",
            )
            try:
                readiness = preflight_birefnet(runtime_settings)
                ai_paths, inference = infer_masks(
                    selected_for_ai,
                    output_root / "ai",
                    runtime_settings,
                    check_control=check_control,
                )
                model = {
                    "id": "birefnet-basic-risk-frames",
                    "provider": inference.get("device") or readiness.get("provider"),
                    "providers": inference.get("providers") or readiness.get("providers"),
                    "precision": inference.get("precision") or readiness.get("precision"),
                    "modelSha256": inference.get("modelSha256")
                    or readiness.get("modelSha256"),
                    "fallbackReason": readiness.get("fallbackReason"),
                    "algorithmVersion": BASIC_ALGORITHM_VERSION,
                }
            except Exception as exc:
                if _is_oom(exc):
                    raise RuntimeError(
                        "Basic 本地处理显存不足（gpu_out_of_memory）；旧版本保持不变。"
                    ) from exc
                warnings.append(f"model-unavailable:{exc}")
                model["fallbackReason"] = str(exc)
                report(
                    "basic-ai",
                    0.30,
                    "本地 BiRefNet 不可用，Basic 将使用确定性色幕降级并记录诊断。",
                )

        outputs: list[str] = []
        frame_diagnostics: list[dict[str, Any]] = []
        result_root = output_root / "rgba"
        result_root.mkdir(parents=True, exist_ok=True)
        for batch_index, (source_index, image) in enumerate(
            zip(selected_indexes, images, strict=True)
        ):
            check_control()
            if settings.material_type == "effect":
                rgba = _effect_rgba(image)
                qc = {"route": "effect", "alphaEnergy": float(rgba[:, :, 3].mean() / 255)}
            else:
                ai_alpha = (
                    read_image(ai_paths[batch_index], cv2.IMREAD_GRAYSCALE)
                    if batch_index in ai_paths
                    else None
                )
                evidence = prepare_frame_evidence(
                    image,
                    chroma_options,
                    ai_alpha=ai_alpha,
                    source_timeline_ordinal=source_index,
                )
                solved = solve_frame_from_evidence(evidence)
                rgba = solved["rgba"]
                qc = dict(solved["qc"])
                qc["aiAssisted"] = ai_alpha is not None
            target = result_root / f"{batch_index:06d}.png"
            if not write_image(target, rgba):
                raise RuntimeError(f"Basic 结果帧 {source_index} 编码失败。")
            decoded = read_image(target, cv2.IMREAD_UNCHANGED)
            if decoded is None or decoded.shape != (*image.shape[:2], 4):
                raise RuntimeError(f"Basic 结果帧 {source_index} 校验失败。")
            outputs.append(str(target))
            frame_diagnostics.append({"index": source_index, "qc": qc})
            report(
                "basic-process",
                0.30 + 0.65 * (batch_index + 1) / len(images),
                None,
            )

        settings_snapshot = settings.model_dump(mode="json") | {
            "algorithmVersion": BASIC_ALGORITHM_VERSION,
            "model": model,
            "warnings": warnings,
        }
        check_control()
        variant = repository.publish_material_variant(
            source_id,
            "basic",
            outputs,
            settings_snapshot,
            expected_revision_id=expected_revision_id,
            source_frame_ids=[str(frame["id"]) for frame in frames],
        )
        report("basic-publish", 1.0, "Basic 不可变素材版本已原子发布。")
        return {
            "variantId": variant["id"],
            "sourceId": source_id,
            "frameCount": len(outputs),
            "model": model,
            "warnings": warnings,
            "frames": frame_diagnostics,
            "elapsedSeconds": time.perf_counter() - started,
        }
    finally:
        shutil.rmtree(output_root, ignore_errors=True)
