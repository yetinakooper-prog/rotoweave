from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .limits import MAX_CORE_IMAGE_UPLOAD_BYTES
from contracts.product import CANONICAL_PIXELS_PER_UNIT


MIN_ANIMATION_CONTENT_SCALE = 0.05
MAX_ANIMATION_CONTENT_SCALE = 4.0
MIN_FRAME_SCALE_CORRECTION = 0.5
MAX_FRAME_SCALE_CORRECTION = 2.0
MAX_EFFECTIVE_CONTENT_SCALE = 4.0
MAX_CORE_IMAGE_EDGE = 8192


def _load_transparent_png(source: Path) -> Image.Image:
    """Load a PNG that contains both transparent background and visible pixels."""

    try:
        with Image.open(source) as opened:
            opened.load()
            if opened.format != "PNG":
                raise ValueError("核心形象图必须是透明 PNG。")
            if opened.width > MAX_CORE_IMAGE_EDGE or opened.height > MAX_CORE_IMAGE_EDGE:
                raise ValueError(
                    f"核心形象图单边不能超过 {MAX_CORE_IMAGE_EDGE} px。"
                )
            if "A" not in opened.getbands() and "transparency" not in opened.info:
                raise ValueError("核心形象图必须包含 Alpha 通道。")
            image = opened.convert("RGBA")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("核心形象图不是可读取的 PNG 图像。") from exc

    minimum, maximum = image.getchannel("A").getextrema()
    if maximum == 0:
        raise ValueError("核心形象图不能全透明。")
    if minimum == 255:
        raise ValueError("核心形象图必须包含透明背景。")
    return image


def prepare_core_reference(
    source: Path,
    target: Path,
) -> dict[str, Any]:
    """Validate and tightly crop an uploaded core image.

    Uploaded references are registered by their visible bottom center at the
    fixed character Pivot (0, 0). Transparent padding therefore cannot change
    calibration or shadow placement.
    """

    image = _load_transparent_png(source)
    bounds = image.getchannel("A").getbbox()
    if not bounds:
        raise ValueError("核心形象图没有可见主体。")
    cropped = image.crop(bounds)

    target.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(target, format="PNG", optimize=True, compress_level=9)
    return {
        "path": str(target),
        "width": int(cropped.width),
        "height": int(cropped.height),
        "origin_x": -float(cropped.width) / 2.0,
        "origin_y": -float(cropped.height),
        "scale": 1.0,
    }


def render_frame_core_reference(
    source: Path,
    target: Path,
    *,
    source_registration: tuple[float, float],
    scale: float,
    offset: tuple[float, float],
) -> dict[str, Any]:
    """Capture one processed frame in the fixed-Pivot character plane."""

    image = _load_transparent_png(source)
    resolved_scale = float(scale)
    if not 0 < resolved_scale <= MAX_EFFECTIVE_CONTENT_SCALE:
        raise ValueError("核心形象帧的有效比例无效。")
    registration_x, registration_y = (
        float(source_registration[0]),
        float(source_registration[1]),
    )
    offset_x, offset_y = float(offset[0]), float(offset[1])

    bounds = image.getchannel("A").getbbox()
    if not bounds:
        raise ValueError("核心形象图没有可见主体。")
    left, top, right, bottom = bounds
    cropped = image.crop((left, top, right, bottom))
    target.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(target, format="PNG", optimize=True, compress_level=9)
    return {
        "path": str(target),
        "width": int(cropped.width),
        "height": int(cropped.height),
        "origin_x": float(left) - registration_x + offset_x / resolved_scale,
        "origin_y": float(top) - registration_y + offset_y / resolved_scale,
        "scale": resolved_scale,
    }
