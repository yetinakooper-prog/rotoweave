from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any, Iterator

import numpy as np


@contextlib.contextmanager
def _source_path(source_path: str | Path) -> Iterator[Path]:
    source = Path(source_path).expanduser().resolve()
    if not (source / "sam3" / "model_builder.py").is_file():
        raise RuntimeError("Pinned local SAM3 source is missing.")
    value = str(source)
    sys.path.insert(0, value)
    try:
        yield source
    finally:
        try:
            sys.path.remove(value)
        except ValueError:
            pass


def _validate_image_hint(
    image_srgb_u8: np.ndarray, alpha_hint: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    image = np.asarray(image_srgb_u8)
    hint = np.asarray(alpha_hint)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise TypeError("SAM3 expects an HxWx3 sRGB uint8 image.")
    if hint.dtype != np.float32 or hint.shape != image.shape[:2]:
        raise TypeError("SAM3 expects a same-size float32 AlphaHint.")
    if not np.isfinite(hint).all():
        raise ValueError("SAM3 AlphaHint contains non-finite values.")
    if float(hint.min()) < 0.0 or float(hint.max()) > 1.0:
        raise RuntimeError("SAM3 AlphaHint must be in [0, 1].")
    return np.ascontiguousarray(image), np.ascontiguousarray(hint)


def _prompt_from_hint(
    hint: np.ndarray, mask_input_size: tuple[int, int] = (288, 288)
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    import cv2

    height, width = hint.shape
    support = hint > 0.02
    ys, xs = np.nonzero(support)
    if xs.size == 0:
        raise RuntimeError("SAM3 AlphaHint has no foreground support.")
    pad = max(2, int(round(max(height, width) * 0.01)))
    box = np.asarray(
        [
            max(0, int(xs.min()) - pad),
            max(0, int(ys.min()) - pad),
            min(width - 1, int(xs.max()) + pad),
            min(height - 1, int(ys.max()) + pad),
        ],
        dtype=np.float32,
    )
    foreground = (hint >= 0.55).astype(np.uint8)
    if not foreground.any():
        foreground[np.unravel_index(int(np.argmax(hint)), hint.shape)] = 1
    distance = cv2.distanceTransform(foreground, cv2.DIST_L2, 5)
    foreground_points: list[tuple[float, float]] = []
    working = distance.copy()
    suppress_radius = max(4, int(round(max(height, width) * 0.08)))
    for _ in range(3):
        index = int(np.argmax(working))
        y, x = np.unravel_index(index, working.shape)
        if float(working[y, x]) <= 0.0:
            break
        foreground_points.append((float(x), float(y)))
        cv2.circle(working, (int(x), int(y)), suppress_radius, 0.0, thickness=-1)
    if not foreground_points:
        y, x = np.unravel_index(int(np.argmax(hint)), hint.shape)
        foreground_points.append((float(x), float(y)))
    background_candidates = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (width // 2, height - 1),
    ]
    background_points = [
        (float(x), float(y))
        for x, y in background_candidates
        if hint[y, x] <= 0.05
    ][:3]
    points = np.asarray(foreground_points + background_points, dtype=np.float32)
    labels = np.asarray(
        [1] * len(foreground_points) + [0] * len(background_points), dtype=np.int32
    )
    clipped = np.clip(hint, 1e-4, 1.0 - 1e-4)
    logits = np.log(clipped / (1.0 - clipped)).astype(np.float32)
    mask_height, mask_width = mask_input_size
    mask_input = cv2.resize(
        logits, (int(mask_width), int(mask_height)), interpolation=cv2.INTER_LINEAR
    )[None]
    return points, labels, box, np.ascontiguousarray(mask_input, dtype=np.float32)


def load_model(
    checkpoint_path: str,
    device: str,
    precision: str,
    source_path: str | Path,
) -> dict[str, Any]:
    if device != "cuda" or precision.lower() not in {"fp16", "float16"}:
        raise RuntimeError("Local SAM3 supports only CUDA FP16.")
    checkpoint = Path(checkpoint_path).resolve()
    if not checkpoint.is_file() or checkpoint.is_symlink():
        raise RuntimeError("Local SAM3 checkpoint is missing.")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    with _source_path(source_path):
        import torch
        from sam3.model_builder import build_sam3_image_model
        from sam3.model.sam3_image_processor import Sam3Processor

        if not torch.cuda.is_available():
            raise RuntimeError("Local SAM3 requires a working NVIDIA CUDA device.")
        try:
            smoke = torch.ones(1, device="cuda", dtype=torch.float32) + 1.0
            torch.cuda.synchronize()
            if float(smoke.item()) != 2.0:
                raise RuntimeError("CUDA smoke computation returned an invalid result.")
        except Exception as exc:
            raise RuntimeError(f"Local SAM3 CUDA smoke computation failed: {exc}") from exc
        model = build_sam3_image_model(
            checkpoint_path=str(checkpoint),
            load_from_HF=False,
            device="cuda",
            eval_mode=True,
            enable_segmentation=True,
            enable_inst_interactivity=True,
            compile=False,
        )
        predictor = model.inst_interactive_predictor
        if predictor is None or not callable(getattr(model, "predict_inst", None)):
            raise RuntimeError("SAM3 checkpoint has no interactive image predictor.")
        processor = Sam3Processor(model, device="cuda", confidence_threshold=0.0)
    mask_input_size = tuple(int(item) for item in predictor.model.sam_prompt_encoder.mask_input_size)
    return {
        "model": model,
        "processor": processor,
        "maskInputSize": mask_input_size,
        "mainCalls": 0,
        "sourcePath": str(Path(source_path).resolve()),
    }


def infer_alpha(
    model: dict[str, Any], image_srgb_u8: np.ndarray, alpha_hint: np.ndarray
) -> np.ndarray:
    image, hint = _validate_image_hint(image_srgb_u8, alpha_hint)
    if float(hint.max()) <= 0.005:
        raise ValueError("SAM3 AlphaHint contains no foreground subject.")
    points, labels, box, mask_input = _prompt_from_hint(
        hint, tuple(model.get("maskInputSize") or (288, 288))
    )
    with _source_path(str(model.get("sourcePath") or "")):
        import torch

        sam3_model = model["model"]
        processor = model["processor"]
        try:
            from PIL import Image

            with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16):
                # Sam3Processor interprets NumPy dimensions as (..., H, W), so
                # pass HWC input as a PIL image to preserve the original extent.
                state = processor.set_image(Image.fromarray(image, mode="RGB"))
                raw_masks, raw_scores, _ = sam3_model.predict_inst(
                    state,
                    point_coords=points,
                    point_labels=labels,
                    box=box,
                    mask_input=mask_input,
                    multimask_output=True,
                    return_logits=True,
                    normalize_coords=True,
                )
            model["mainCalls"] = int(model.get("mainCalls") or 0) + 1
        except torch.cuda.OutOfMemoryError as exc:
            raise RuntimeError("SAM3 CUDA out of memory.") from exc
    masks = np.asarray(raw_masks, dtype=np.float32)
    scores = np.asarray(raw_scores, dtype=np.float32).reshape(-1)
    if masks.ndim != 3 or masks.shape[1:] != hint.shape or masks.shape[0] == 0:
        raise RuntimeError(
            "SAM3 returned invalid full-resolution mask logits: "
            f"masks={masks.shape}, scores={np.asarray(raw_scores).shape}, expected=(*,{hint.shape[0]},{hint.shape[1]})."
        )
    if not np.isfinite(masks).all() or not np.isfinite(scores).all():
        raise RuntimeError("SAM3 returned non-finite prediction tensors.")
    probabilities = 1.0 / (1.0 + np.exp(-np.clip(masks, -30.0, 30.0)))
    hint_binary = hint >= 0.5
    candidate_binary = probabilities >= 0.5
    intersection = np.sum(candidate_binary & hint_binary[None], axis=(1, 2), dtype=np.float64)
    denominator = np.sum(candidate_binary, axis=(1, 2), dtype=np.float64) + float(
        np.sum(hint_binary)
    )
    dice = np.divide(2.0 * intersection, np.maximum(denominator, 1.0))
    score_quality = np.clip(scores[: probabilities.shape[0]], 0.0, 1.0)
    ranking = 0.75 * dice + 0.25 * score_quality
    selected = probabilities[int(np.argmax(ranking))]
    result = np.ascontiguousarray(selected, dtype=np.float32)
    if result.shape != hint.shape or not np.isfinite(result).all():
        raise RuntimeError("SAM3 produced an invalid Alpha result.")
    if float(result.min()) < 0.0 or float(result.max()) > 1.0:
        raise RuntimeError("SAM3 Alpha result is outside [0, 1].")
    return result


__all__ = ["infer_alpha", "load_model"]
