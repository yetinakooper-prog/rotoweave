from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np
from PIL import Image

from .shadows import measure_shadow_alpha, resolve_shadow_sequence
from .workspace_format import WorkspaceFormatError, resolve_workspace_path


def _transformed_alpha(
    path: Path,
    *,
    scale_x: float,
    scale_y: float,
    rotation_degrees: float,
) -> tuple[np.ndarray, tuple[float, float]]:
    with Image.open(path) as opened:
        alpha = np.asarray(opened.convert("RGBA").getchannel("A"), dtype=np.uint8)
    height, width = alpha.shape
    sx = float(scale_x)
    sy = float(scale_y)
    angle = math.radians(float(rotation_degrees))
    cosine = math.cos(angle)
    sine = math.sin(angle)
    linear = np.array(
        [[cosine * sx, sine * sy], [-sine * sx, cosine * sy]],
        dtype=np.float64,
    )
    pivot = np.array([width / 2.0, float(height)], dtype=np.float64)
    corners = np.array(
        [[0.0, 0.0], [float(width), 0.0], [0.0, float(height)], [float(width), float(height)]],
        dtype=np.float64,
    )
    transformed = (corners - pivot) @ linear.T
    minimum = transformed.min(axis=0)
    maximum = transformed.max(axis=0)
    padding = 4.0
    output_width = max(1, int(math.ceil(maximum[0] - minimum[0] + padding * 2)))
    output_height = max(1, int(math.ceil(maximum[1] - minimum[1] + padding * 2)))
    pivot_output = np.array([-minimum[0] + padding, -minimum[1] + padding])
    translation = pivot_output - linear @ pivot
    matrix = np.array(
        [
            [linear[0, 0], linear[0, 1], translation[0]],
            [linear[1, 0], linear[1, 1], translation[1]],
        ],
        dtype=np.float64,
    )
    warped = cv2.warpAffine(
        alpha,
        matrix,
        (output_width, output_height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return warped, (float(pivot_output[0]), float(pivot_output[1]))


def _resolved_shadow_settings(
    character: dict[str, Any], override: dict[str, Any] | None
) -> dict[str, Any]:
    value = {**(character.get("shadow") or {}), **(override or {})}
    value.setdefault("enabled", False)
    value.setdefault("color", "#000000")
    value.setdefault("baseOpacity", 0.0)
    value.setdefault("lightAngleDegrees", 135.0)
    return value


def resolve_domain_action_shadows(
    root: Path,
    domain: dict[str, Any],
    character: dict[str, Any],
    frame_refs: Sequence[dict[str, Any]],
    *,
    loop: bool,
    shadow_standard_y: float | None = None,
    shadow_override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    variants = {str(item.get("id") or ""): item for item in domain.get("materialVariants") or []}
    shadow = _resolved_shadow_settings(character, shadow_override)
    standard_y = float(
        shadow_standard_y
        if shadow_standard_y is not None
        else (character.get("calibration") or {}).get("shadowStandardY", 0.0)
    )
    measurements: list[dict[str, float] | None] = []
    adjustments: list[dict[str, float]] = []
    resolved_frame_settings: list[tuple[bool, float]] = []
    for ref in frame_refs:
        variant = variants.get(str(ref.get("variantId") or ""))
        frame = next(
            (item for item in (variant or {}).get("frames") or [] if item.get("id") == ref.get("frameId")),
            None,
        )
        if frame is None:
            raise WorkspaceFormatError("智能阴影引用了不存在的处理帧。")
        transform = ref.get("transform") or {}
        scale = transform.get("scale") or {}
        scale_x = float(scale.get("x", 1.0))
        scale_y = float(scale.get("y", 1.0))
        alpha, pivot = _transformed_alpha(
            resolve_workspace_path(root, str(frame.get("path") or "")),
            scale_x=scale_x,
            scale_y=scale_y,
            rotation_degrees=float(transform.get("rotationDegrees", 0.0)),
        )
        position = transform.get("position") or {}
        measurement = measure_shadow_alpha(
            alpha,
            pivot,
            offset_px=(float(position.get("x", 0.0)), -float(position.get("y", 0.0))),
            light_angle_degrees=float(shadow["lightAngleDegrees"]),
            ground_relative_down_px=standard_y,
        )
        if measurement is not None:
            measurement["casterOpacity"] = min(
                1.0,
                max(0.0, float(transform.get("opacity", 1.0))),
            )
        measurements.append(measurement)
        correction = (transform.get("shadow") or {})
        correction_scale = correction.get("scale") or {}
        correction_offset = correction.get("offset") or {}
        adjustments.append(
            {
                "widthScale": float(correction_scale.get("x", 1.0)),
                "depthScale": float(correction_scale.get("y", 1.0)),
                "offsetX": float(correction_offset.get("x", 0.0)),
                "offsetY": float(correction_offset.get("y", 0.0)),
            }
        )
        enabled = correction.get("enabled")
        opacity = correction.get("opacity")
        resolved_frame_settings.append(
            (
                bool(shadow["enabled"] if enabled is None else enabled),
                float(shadow["baseOpacity"] if opacity is None else opacity),
            )
        )
    resolved = resolve_shadow_sequence(
        measurements,
        mode="auto",
        loop=loop,
        opacity=1.0,
        light_angle_degrees=float(shadow["lightAngleDegrees"]),
        adjustments=adjustments,
    )
    for item, (enabled, opacity) in zip(resolved, resolved_frame_settings, strict=True):
        item["alpha"] = round(float(item["alpha"]) * opacity, 6) if enabled else 0.0
    return resolved


def resolve_domain_core_shadow(
    root: Path,
    character: dict[str, Any],
    *,
    shadow_standard_y: float | None = None,
    shadow_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    core = (character.get("calibration") or {}).get("coreReference")
    if not isinstance(core, dict):
        return None
    shadow = _resolved_shadow_settings(character, shadow_override)
    scale = float(core.get("scale", 1.0))
    alpha, pivot = _transformed_alpha(
        resolve_workspace_path(root, str(core.get("path") or "")),
        scale_x=scale,
        scale_y=scale,
        rotation_degrees=0.0,
    )
    origin = core.get("origin") or {}
    pivot_x = float(origin.get("x", 0.0)) * scale + float(core.get("width", 0)) * scale / 2.0
    pivot_down = float(origin.get("y", 0.0)) * scale + float(core.get("height", 0)) * scale
    standard_y = float(
        shadow_standard_y
        if shadow_standard_y is not None
        else (character.get("calibration") or {}).get("shadowStandardY", 0.0)
    )
    measurement = measure_shadow_alpha(
        alpha,
        pivot,
        offset_px=(pivot_x, pivot_down),
        light_angle_degrees=float(shadow["lightAngleDegrees"]),
        ground_relative_down_px=standard_y,
    )
    resolved = resolve_shadow_sequence(
        [measurement],
        mode="auto",
        loop=False,
        opacity=float(shadow["baseOpacity"] if shadow["enabled"] else 0.0),
        light_angle_degrees=float(shadow["lightAngleDegrees"]),
    )
    return resolved[0] if resolved else None
