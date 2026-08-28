from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image

from .config import Settings
from .inference_runtime import ModelSpec, get_model_runtime, start_model_warmup


def _spec(settings: Settings, model_path: Path | None = None) -> ModelSpec:
    return ModelSpec(
        "birefnet",
        (model_path or settings.bundled_birefnet_onnx).resolve(),
        "fp32",
    )


def infer_masks(
    sources: list[tuple[int, Path]],
    output_dir: Path,
    settings: Settings,
    timeout_seconds: int = 900,
    check_control: Callable[[], None] | None = None,
) -> tuple[dict[int, Path], dict[str, Any]]:
    """Run the current bundled ONNX model."""
    if not sources:
        return {}, {"completed": 0, "device": "skipped", "runtime": "qc-gated"}
    if not settings.birefnet_available:
        raise RuntimeError("未检测到内置 BiRefNet 模型。")
    return _infer_masks_onnx(
        sources,
        output_dir,
        settings.bundled_birefnet_onnx,
        settings,
        check_control=check_control,
    )


def preflight_birefnet(settings: Settings) -> dict[str, Any]:
    """Verify the bundled model and create a usable inference session.

    High-quality matting calls this before writing any generation output so a
    missing/corrupt model or unusable execution provider cannot silently turn a
    High job into a chroma-only result.
    """

    if not settings.birefnet_available:
        raise RuntimeError("未检测到内置 BiRefNet 模型，请重新安装 RotoWeave。")
    runtime = get_model_runtime(_spec(settings), settings.runtime_root)
    runtime.ensure_ready()
    snapshot = runtime.snapshot()
    return {
        **snapshot,
        "device": snapshot.get("provider"),
    }


def birefnet_health(settings: Settings) -> dict[str, Any]:
    """Report actual session/provider state instead of model-file presence."""

    if not settings.birefnet_available:
        return {
            "available": False,
            "mode": "missing",
            "state": "error",
            "provider": None,
            "precision": "fp32",
            "fallbackReason": "BiRefNet 模型文件缺失",
        }
    try:
        runtime = start_model_warmup(_spec(settings), settings.runtime_root)
        snapshot = runtime.snapshot()
    except Exception as exc:
        return {
            "available": False,
            "mode": "error",
            "state": "error",
            "provider": None,
            "precision": "fp32",
            "fallbackReason": None,
            "error": str(exc),
        }
    return {
        **snapshot,
        "available": snapshot["state"] in {"ready", "degraded"},
        "mode": (
            "cuda"
            if snapshot.get("provider") == "CUDAExecutionProvider"
            else "cpu"
        ),
    }


def _infer_masks_onnx(
    sources: list[tuple[int, Path]],
    output_dir: Path,
    model_path: Path,
    settings: Settings,
    *,
    check_control: Callable[[], None] | None = None,
) -> tuple[dict[int, Path], dict[str, Any]]:
    runtime = get_model_runtime(_spec(settings, model_path), settings.runtime_root)
    runtime.ensure_ready()
    runtime_metadata = runtime.snapshot()
    model_input = runtime.inputs()[0]
    input_height = int(model_input.shape[-2]) if isinstance(model_input.shape[-2], int) else 1024
    input_width = int(model_input.shape[-1]) if isinstance(model_input.shape[-1], int) else 1024
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 1, 3)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 1, 3)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_paths: dict[int, Path] = {}

    for frame_index, source_path in sources:
        if check_control is not None:
            check_control()
        with Image.open(source_path) as opened:
            source = np.asarray(opened.convert("RGB"))
        resized = cv2.resize(source, (input_width, input_height), interpolation=cv2.INTER_AREA)
        tensor = ((resized.astype(np.float32) / 255.0 - mean) / std).transpose(2, 0, 1)[None]
        outputs = runtime.run({model_input.name: tensor})
        prediction = np.asarray(outputs[-1], dtype=np.float32).squeeze()
        if prediction.ndim != 2:
            prediction = prediction.reshape(prediction.shape[-2], prediction.shape[-1])
        if float(prediction.min()) < 0.0 or float(prediction.max()) > 1.0:
            prediction = 1.0 / (1.0 + np.exp(-np.clip(prediction, -30.0, 30.0)))
        prediction = cv2.resize(
            prediction,
            (source.shape[1], source.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        target = output_dir / f"{frame_index:06d}.png"
        Image.fromarray((np.clip(prediction, 0, 1) * 255).astype(np.uint8), "L").save(target)
        result_paths[frame_index] = target
        if check_control is not None:
            check_control()

    return result_paths, {
        "completed": len(result_paths),
        "device": runtime_metadata.get("provider") or "unknown",
        "providers": runtime_metadata.get("providers") or [],
        "precision": runtime_metadata.get("precision") or "fp32",
        "model": model_path.name,
        "modelRevision": runtime_metadata.get("modelRevision"),
        "modelSha256": runtime_metadata.get("modelSha256"),
        "runtime": "onnxruntime",
    }
