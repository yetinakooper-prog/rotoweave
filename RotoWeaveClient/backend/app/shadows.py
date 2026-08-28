from __future__ import annotations

import math
from statistics import median
from typing import Any, Sequence

import cv2
import numpy as np


ALPHA_THRESHOLD = 16
DEFAULT_SHADOW_COLOR = "#000000"
DEFAULT_SHADOW_OPACITY = 0.35
DEFAULT_LIGHT_ANGLE_DEGREES = 135.0
SHADOW_MODES = {"auto", "grounded", "flying"}
_ROBUST_PERCENTILES = (3.0, 97.0)
_MIN_LIGHT_ELEVATION_RADIANS = math.radians(15.0)
_COMPONENT_GLOBAL_KEEP_RATIO = 0.12
_COMPONENT_LOCAL_KEEP_RATIO = 0.005
_COMPONENT_NEARBY_RATIO = 0.04
_TEMPORAL_SIZE_RATIO_LIMIT = 1.8


def validate_light_angle(light_angle_degrees: float) -> float:
    """Validate the current upper-half-circle shadow-light contract."""
    value = float(light_angle_degrees)
    if not math.isfinite(value) or not 0.0 <= value <= 180.0:
        raise ValueError("光源角度必须是 0 到 180 度之间的有限数值。")
    return value


def shadow_rotation_degrees(light_angle_degrees: float) -> float:
    """New exports always use a ground-attached ellipse with a horizontal long axis."""

    validate_light_angle(light_angle_degrees)
    return 0.0


def _projection_slope(light_angle_degrees: float) -> float:
    angle = math.radians(validate_light_angle(light_angle_degrees))
    denominator = max(math.sin(angle), math.sin(_MIN_LIGHT_ELEVATION_RADIANS))
    return max(-1.5, min(1.5, math.cos(angle) / denominator))


def _retained_mask(alpha: np.ndarray) -> np.ndarray:
    source = (alpha > ALPHA_THRESHOLD).astype(np.uint8)
    if not np.any(source):
        return source
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        source, connectivity=8
    )
    if component_count <= 2:
        return source
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = int(areas.max(initial=0))
    primary_label = int(np.argmax(areas)) + 1
    primary = stats[primary_label]
    primary_left = int(primary[cv2.CC_STAT_LEFT])
    primary_top = int(primary[cv2.CC_STAT_TOP])
    primary_right = primary_left + int(primary[cv2.CC_STAT_WIDTH]) - 1
    primary_bottom = primary_top + int(primary[cv2.CC_STAT_HEIGHT]) - 1
    nearby_distance = max(
        3,
        int(round(max(source.shape) * _COMPONENT_NEARBY_RATIO)),
    )
    minimum_local_area = max(
        4,
        int(math.ceil(largest * _COMPONENT_LOCAL_KEEP_RATIO)),
    )
    retained = np.zeros_like(source)
    for label in range(1, component_count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        left = int(stats[label, cv2.CC_STAT_LEFT])
        top = int(stats[label, cv2.CC_STAT_TOP])
        right = left + int(stats[label, cv2.CC_STAT_WIDTH]) - 1
        bottom = top + int(stats[label, cv2.CC_STAT_HEIGHT]) - 1
        gap_x = max(primary_left - right - 1, left - primary_right - 1, 0)
        gap_y = max(primary_top - bottom - 1, top - primary_bottom - 1, 0)
        close_to_primary = math.hypot(gap_x, gap_y) <= nearby_distance
        globally_significant = area >= largest * _COMPONENT_GLOBAL_KEEP_RATIO
        if (
            label == primary_label
            or globally_significant
            or (area >= minimum_local_area and close_to_primary)
        ):
            retained[labels == label] = 1
    return retained


def _weighted_percentile(
    values: np.ndarray,
    weights: np.ndarray,
    percentile: float,
) -> float:
    if values.size == 0:
        raise ValueError("加权分位数需要至少一个样本。")
    resolved_weights = np.maximum(0.0, weights.astype(np.float64, copy=False))
    total = float(resolved_weights.sum())
    if total <= 0.0:
        return float(np.percentile(values.astype(np.float64), percentile))
    order = np.argsort(values, kind="stable")
    ordered_values = values[order].astype(np.float64, copy=False)
    cumulative = np.cumsum(resolved_weights[order])
    target = total * min(100.0, max(0.0, float(percentile))) / 100.0
    index = min(int(np.searchsorted(cumulative, target, side="left")), values.size - 1)
    return float(ordered_values[index])


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    total = float(np.maximum(0.0, weights).sum())
    if total <= 0.0:
        return float(np.mean(values.astype(np.float64)))
    return float(np.average(values.astype(np.float64), weights=weights))


def _contact_columns(mask: np.ndarray, lower_edge: float, body_height: float) -> np.ndarray:
    band_height = max(1, int(math.ceil(body_height * 0.12)))
    top = max(0, int(math.floor(lower_edge)) - band_height + 1)
    bottom = min(mask.shape[0], int(math.ceil(lower_edge)) + 1)
    counts = mask[top:bottom, :].sum(axis=0)
    occupied = np.flatnonzero(counts > 0)
    if occupied.size == 0:
        return occupied

    gap_tolerance = max(1, int(round(mask.shape[1] * 0.02)))
    runs: list[np.ndarray] = []
    start = 0
    for index in range(1, occupied.size):
        if int(occupied[index] - occupied[index - 1]) > gap_tolerance + 1:
            runs.append(occupied[start:index])
            start = index
    runs.append(occupied[start:])
    masses = [int(counts[run].sum()) for run in runs]
    minimum_mass = max(1, int(math.ceil(max(masses, default=1) * 0.05)))
    kept = [run for run, mass in zip(runs, masses, strict=True) if mass >= minimum_mass]
    return np.concatenate(kept) if kept else occupied


def measure_alpha_support_y(alpha: np.ndarray) -> float | None:
    """Return the robust visible support line in top-left image coordinates."""

    if alpha.ndim != 2:
        raise ValueError("支撑线分析需要单通道 Alpha。")
    mask = _retained_mask(alpha)
    ys, _ = np.nonzero(mask)
    if ys.size == 0:
        return None
    occupied_columns = np.flatnonzero(mask.any(axis=0))
    bottom_by_column = np.array(
        [int(np.flatnonzero(mask[:, x])[-1]) for x in occupied_columns],
        dtype=np.float64,
    )
    lower_edge = float(np.percentile(bottom_by_column, 95.0))
    top = int(ys.min())
    body_height = float(max(1.0, lower_edge - top + 1.0))
    contact_columns = _contact_columns(mask, lower_edge, body_height)
    if contact_columns.size == 0:
        return lower_edge
    contact_bottoms = np.array(
        [int(np.flatnonzero(mask[:, x])[-1]) for x in contact_columns],
        dtype=np.float64,
    )
    return float(np.percentile(contact_bottoms, 75.0))


def measure_shadow_alpha(
    alpha: np.ndarray,
    pivot_px: tuple[float, float],
    *,
    scale: float = 1.0,
    offset_px: tuple[float, float] = (0.0, 0.0),
    light_angle_degrees: float = DEFAULT_LIGHT_ANGLE_DEGREES,
    ground_relative_down_px: float = 0.0,
) -> dict[str, float] | None:
    """Measure robust contact, clearance and parallel-projection evidence for one frame.

    Input image coordinates use +Y downward. Only retained pixels on or above the
    receiving plane participate. Returned X values are pivot-relative and
    right-positive. Vertical values are magnitudes; the resolver converts the final
    ground offset to Unity's +Y-up convention.
    """

    if alpha.ndim != 2:
        raise ValueError("阴影分析需要单通道 Alpha。")
    mask = _retained_mask(alpha)
    alpha_weights = alpha.astype(np.float64) / 255.0 * mask
    source_ys, _ = np.nonzero(mask)
    if source_ys.size == 0:
        return None

    resolved_scale = max(0.000001, float(scale))
    offset_x, offset_y = float(offset_px[0]), float(offset_px[1])
    pivot_x, pivot_y = float(pivot_px[0]), float(pivot_px[1])
    ground_down = float(ground_relative_down_px)

    # The shadow standard Y is the receiving plane, not only a display guide.
    # Pixels below it would be behind the plane and must not widen the caster.
    row_positions_down = (
        (np.arange(mask.shape[0], dtype=np.float64) - pivot_y) * resolved_scale
        + offset_y
    )
    caster_mask = mask.copy()
    caster_mask[row_positions_down > ground_down, :] = 0
    caster_weights = alpha_weights.copy()
    caster_weights[row_positions_down > ground_down, :] = 0.0
    ys, xs = np.nonzero(caster_mask)
    if xs.size == 0:
        return None
    sample_weights = caster_weights[ys, xs]

    occupied_columns = np.flatnonzero(caster_mask.any(axis=0))
    bottom_by_column = np.array(
        [int(np.flatnonzero(caster_mask[:, x])[-1]) for x in occupied_columns],
        dtype=np.float64,
    )
    lower_edge = float(np.percentile(bottom_by_column, 95.0))
    top = int(ys.min())
    unscaled_body_height = float(max(1.0, lower_edge - top + 1.0))
    ground_source_y = pivot_y + (ground_down - offset_y) / resolved_scale
    columns = _contact_columns(caster_mask, ground_source_y, unscaled_body_height)
    if columns.size == 0:
        # Airborne silhouettes do not reach the receiving plane. Their lowest
        # valid region still provides a stable horizontal center estimate.
        columns = _contact_columns(caster_mask, lower_edge, unscaled_body_height)

    robust_left, robust_right = (
        _weighted_percentile(xs, sample_weights, percentile)
        for percentile in _ROBUST_PERCENTILES
    )
    unscaled_body_width = max(1.0, robust_right - robust_left + 1.0)

    contact_band_height = max(1, int(math.ceil(unscaled_body_height * 0.14)))
    contact_edge = ground_source_y if columns.size else lower_edge
    contact_top = max(0, int(math.floor(contact_edge)) - contact_band_height + 1)
    contact_bottom = min(caster_mask.shape[0], int(math.ceil(contact_edge)) + 1)
    contact_ys, contact_xs = np.nonzero(caster_mask[contact_top:contact_bottom, :])
    if contact_xs.size:
        contact_ys = contact_ys + contact_top
        vertical_weight = (
            0.35
            + 0.65
            * np.clip(
                1.0
                - (contact_edge - contact_ys.astype(np.float64))
                / max(1.0, float(contact_band_height)),
                0.0,
                1.0,
            )
        )
        contact_weights = caster_weights[contact_ys, contact_xs] * vertical_weight
        contact_left = _weighted_percentile(contact_xs, contact_weights, 5.0)
        contact_right = _weighted_percentile(contact_xs, contact_weights, 95.0)
        contact_center = _weighted_mean(
            np.clip(contact_xs.astype(np.float64), contact_left, contact_right),
            contact_weights,
        )
        contact_span = max(1.0, contact_right - contact_left + 1.0)
        contact_fill = min(
            1.0,
            float(contact_weights.sum())
            / max(contact_span * contact_band_height, 1.0),
        )
    else:
        contact_center = (robust_left + robust_right) / 2.0
        contact_span = max(1.0, unscaled_body_width * 0.5)
        contact_fill = 0.0

    lower_cutoff = top + unscaled_body_height * 0.42
    lower_samples = ys.astype(np.float64) >= lower_cutoff
    lower_xs = xs[lower_samples] if np.any(lower_samples) else xs
    lower_weights = sample_weights[lower_samples] if np.any(lower_samples) else sample_weights
    lower_left = _weighted_percentile(lower_xs, lower_weights, 5.0)
    lower_right = _weighted_percentile(lower_xs, lower_weights, 95.0)
    lower_mass_width = max(1.0, lower_right - lower_left + 1.0)
    fill_ratio = min(
        1.0,
        float(sample_weights.sum())
        / max(unscaled_body_width * unscaled_body_height, 1.0),
    )
    mean_alpha = min(1.0, max(0.0, float(sample_weights.mean())))

    body_left_x = (robust_left - pivot_x) * resolved_scale + offset_x
    body_right_x = (robust_right - pivot_x) * resolved_scale + offset_x
    body_height = unscaled_body_height * resolved_scale

    slope = _projection_slope(light_angle_degrees)
    relative_x = (xs.astype(np.float64) - pivot_x) * resolved_scale + offset_x
    pixel_positions_down = (
        (ys.astype(np.float64) - pivot_y) * resolved_scale + offset_y
    )
    local_height = np.maximum(0.0, ground_down - pixel_positions_down)
    projected_x = relative_x + local_height * slope * 0.30
    projected_left, projected_right = (
        _weighted_percentile(projected_x, sample_weights, percentile)
        for percentile in _ROBUST_PERCENTILES
    )
    if slope > 0.000001:
        projection_extension = max(0.0, projected_right - body_right_x)
    elif slope < -0.000001:
        projection_extension = max(0.0, body_left_x - projected_left)
    else:
        projection_extension = 0.0

    bottom_relative_down = (lower_edge - pivot_y) * resolved_scale + offset_y
    tolerance = max(1.0, body_height * 0.02)
    clearance = max(0.0, ground_down - bottom_relative_down - tolerance)
    contact_confidence = math.exp(
        -clearance / max(body_height * 0.12, 1.0)
    ) * (0.65 + contact_fill * 0.35)
    return {
        "contactCenterX": (contact_center - pivot_x) * resolved_scale + offset_x,
        "bodyCenterX": ((robust_left + robust_right) / 2.0 - pivot_x)
        * resolved_scale
        + offset_x,
        "contactSpan": contact_span * resolved_scale,
        "bodyWidth": unscaled_body_width * resolved_scale,
        "massWidth": lower_mass_width * resolved_scale,
        "silhouetteWidth": unscaled_body_width * resolved_scale,
        "bodyHeight": body_height,
        "airbornePx": clearance,
        "groundPositionY": -ground_down,
        "projectionExtension": projection_extension,
        "fillRatio": fill_ratio,
        "meanAlpha": mean_alpha,
        "contactConfidence": min(1.0, max(0.0, contact_confidence)),
        "casterOpacity": 1.0,
    }


def _nearest_measurements(
    measurements: Sequence[dict[str, float] | None],
) -> list[dict[str, float]]:
    valid = [item for item in measurements if item is not None]
    if not valid:
        fallback = {
            "contactCenterX": 0.0,
            "bodyCenterX": 0.0,
            "contactSpan": 1.0,
            "bodyWidth": 1.0,
            "massWidth": 1.0,
            "silhouetteWidth": 1.0,
            "bodyHeight": 1.0,
            "airbornePx": 0.0,
            "groundPositionY": 0.0,
            "projectionExtension": 0.0,
            "fillRatio": 1.0,
            "meanAlpha": 1.0,
            "contactConfidence": 1.0,
            "casterOpacity": 1.0,
        }
        return [dict(fallback) for _ in measurements]
    result: list[dict[str, float]] = []
    for index, item in enumerate(measurements):
        if item is not None:
            result.append(item)
            continue
        nearest = min(
            (
                candidate_index
                for candidate_index, candidate in enumerate(measurements)
                if candidate is not None
            ),
            key=lambda candidate_index: abs(candidate_index - index),
        )
        result.append(measurements[nearest] or valid[0])
    return result


def _window_indices(count: int, index: int, loop: bool) -> list[int]:
    if count <= 2:
        return [index]
    if loop:
        return [(index - 1) % count, index, (index + 1) % count]
    return [max(0, index - 1), index, min(count - 1, index + 1)]


def _measurements_are_comparable(
    current: dict[str, float],
    candidate: dict[str, float],
) -> bool:
    for key in ("bodyWidth", "bodyHeight"):
        first = max(0.000001, float(current[key]))
        second = max(0.000001, float(candidate[key]))
        ratio = max(first, second) / min(first, second)
        if ratio > _TEMPORAL_SIZE_RATIO_LIMIT:
            return False
    return True


def _smoothstep(value: float) -> float:
    clamped = max(0.0, min(1.0, float(value)))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _clean(value: float) -> float:
    return round(float(value), 6)


def resolve_shadow_sequence(
    measurements: Sequence[dict[str, float] | None],
    *,
    mode: str,
    loop: bool,
    opacity: float,
    light_angle_degrees: float,
    adjustments: Sequence[dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve one stable, horizontal and ground-attached ellipse per frame."""

    if mode not in SHADOW_MODES:
        raise ValueError("阴影模式必须为 auto、grounded 或 flying。")
    if not measurements:
        return []
    filled = _nearest_measurements(measurements)
    raw: list[dict[str, float]] = []
    for item in filled:
        body_height = max(1.0, float(item["bodyHeight"]))
        clearance = max(0.0, float(item["airbornePx"]))
        if mode == "grounded":
            clearance = 0.0
        elif mode == "flying":
            clearance = max(clearance, body_height * 0.20)
        raw.append(
            {
                "centerX": float(item["contactCenterX"]),
                "contactSpan": max(1.0, float(item["contactSpan"])),
                "bodyWidth": max(1.0, float(item["bodyWidth"])),
                "massWidth": max(
                    1.0,
                    float(item.get("massWidth", item["bodyWidth"])),
                ),
                "silhouetteWidth": max(
                    1.0,
                    float(item.get("silhouetteWidth", item["bodyWidth"])),
                ),
                "bodyHeight": body_height,
                "clearancePx": clearance,
                "groundPositionY": float(item.get("groundPositionY", 0.0)),
                "projectionExtension": max(
                    0.0, float(item.get("projectionExtension", 0.0))
                ),
                "fillRatio": min(
                    1.0,
                    max(0.0, float(item.get("fillRatio", 0.55))),
                ),
                "meanAlpha": min(
                    1.0,
                    max(0.0, float(item.get("meanAlpha", 1.0))),
                ),
                "contactConfidence": min(
                    1.0,
                    max(0.0, float(item.get("contactConfidence", 1.0))),
                ),
                "casterOpacity": min(
                    1.0,
                    max(0.0, float(item.get("casterOpacity", 1.0))),
                ),
            }
        )

    smoothed: list[dict[str, float]] = []
    keys = (
        "centerX",
        "contactSpan",
        "bodyWidth",
        "massWidth",
        "silhouetteWidth",
        "bodyHeight",
        "clearancePx",
        "groundPositionY",
        "projectionExtension",
        "fillRatio",
        "meanAlpha",
        "contactConfidence",
    )
    for index, current in enumerate(raw):
        comparable = [
            raw[candidate_index]
            for candidate_index in _window_indices(len(raw), index, loop)
            if _measurements_are_comparable(current, raw[candidate_index])
        ]
        if not comparable:
            comparable = [current]
        smoothed_item = {
            key: float(median([item[key] for item in comparable]))
            for key in keys
        }
        # Action opacity is an explicit current-frame command, not noisy image
        # evidence. Temporal filtering must never erase a deliberate opacity key.
        smoothed_item["casterOpacity"] = current["casterOpacity"]
        smoothed.append(smoothed_item)

    slope = _projection_slope(light_angle_degrees)
    projection_direction = 1.0 if slope > 0.000001 else -1.0 if slope < -0.000001 else 0.0
    angle_radians = math.radians(validate_light_angle(light_angle_degrees))
    light_elevation = max(0.0, math.sin(angle_radians))
    base_opacity = min(1.0, max(0.0, float(opacity)))
    corrections = list(adjustments or [])
    while len(corrections) < len(smoothed):
        corrections.append({})

    result: list[dict[str, Any]] = []
    for item, correction in zip(smoothed, corrections, strict=True):
        silhouette_width = item["silhouetteWidth"]
        mass_width = min(silhouette_width, item["massWidth"])
        minimum_width = max(item["contactSpan"] * 1.05, mass_width * 0.78)
        maximum_width = max(minimum_width, silhouette_width * 0.98)
        base_width = max(item["contactSpan"] * 1.20, mass_width * 0.95)
        base_width = max(minimum_width, min(maximum_width, base_width))

        density = _smoothstep(item["fillRatio"] / 0.60)
        thickness_ratio = 0.12 + density * 0.07
        base_depth = max(
            0.25,
            min(item["bodyHeight"] * 0.115, base_width * thickness_ratio),
        )
        extension = min(
            item["projectionExtension"] * 0.62,
            silhouette_width * 0.55,
            base_width * 0.80,
        )

        height_ratio = item["clearancePx"] / max(item["bodyHeight"] * 0.75, 0.000001)
        airborne_ratio = _smoothstep(height_ratio)
        grazing_ratio = 1.0 - light_elevation
        width = (base_width + extension) * (1.0 - airborne_ratio * 0.35)
        depth = (
            base_depth
            * (1.0 - airborne_ratio * 0.40)
            * (1.0 - grazing_ratio * 0.12)
        )
        width *= max(0.05, min(8.0, float(correction.get("widthScale", 1.0))))
        depth *= max(0.05, min(8.0, float(correction.get("depthScale", 1.0))))
        position_x = (
            item["centerX"]
            + projection_direction * extension * 0.5
            + float(correction.get("offsetX", 0.0))
        )
        position_y = (
            item["groundPositionY"]
            - depth * 0.08
            + float(correction.get("offsetY", 0.0))
        )
        material_alpha_factor = 0.35 + 0.65 * math.sqrt(item["meanAlpha"])
        density_alpha_factor = 0.78 + 0.22 * math.sqrt(
            min(1.0, item["fillRatio"] / 0.60)
        )
        contact_alpha_factor = 0.90 + 0.10 * min(
            1.0,
            item["contactConfidence"] / 0.80,
        )
        light_alpha_factor = 0.78 + 0.22 * light_elevation
        caster_alpha_factor = math.sqrt(item["casterOpacity"])
        alpha = (
            base_opacity
            * material_alpha_factor
            * density_alpha_factor
            * contact_alpha_factor
            * light_alpha_factor
            * caster_alpha_factor
            * (1.0 - airborne_ratio * 0.60)
        )
        result.append(
            {
                "positionPx": [_clean(position_x), _clean(position_y)],
                "widthPx": _clean(max(1.0, width)),
                "depthPx": _clean(max(0.25, depth)),
                "rotationDegrees": 0.0,
                "alpha": _clean(min(1.0, max(0.0, alpha))),
                "airborneRatio": _clean(airborne_ratio),
            }
        )
    return result
