from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

from .versions import HYBRID_MATTE_VERSION, CHROMA_COLOR_RECOVERY_REVISION

_SRGB_AXIS = np.arange(256, dtype=np.float32) / 255.0
_SRGB_TO_LINEAR_LUT = np.where(
    _SRGB_AXIS <= 0.04045,
    _SRGB_AXIS / 12.92,
    ((_SRGB_AXIS + 0.055) / 1.055) ** 2.4,
).astype(np.float32)


def _border_regions(image: np.ndarray) -> list[np.ndarray]:
    height, width = image.shape[:2]
    band = max(2, min(height, width) // 40)
    return [
        image[:band, :, :3].reshape(-1, 3),
        image[-band:, :, :3].reshape(-1, 3),
        image[:, :band, :3].reshape(-1, 3),
        image[:, -band:, :3].reshape(-1, 3),
    ]


def _prefer_screen_pixels(pixels: np.ndarray) -> np.ndarray:
    # A screen may be green, blue, red, cyan, magenta or yellow.  Prefer
    # chromatic border pixels without assigning special status to one channel.
    values = pixels.astype(np.float32)
    channel_range = values.max(axis=1) - values.min(axis=1)
    saturated = channel_range > 18.0
    return pixels[saturated] if np.any(saturated) else pixels


def estimate_background_palette_bgr(image: np.ndarray, max_samples: int = 5) -> np.ndarray:
    regions = _border_regions(image)
    combined = _prefer_screen_pixels(np.concatenate(regions, axis=0))
    palette: list[np.ndarray] = []
    if len(combined):
        # Split border pixels by luminance so shadows and compressed highlights
        # become independent key samples instead of collapsing into one median.
        luminance = cv2.cvtColor(combined.reshape(-1, 1, 3), cv2.COLOR_BGR2YCrCb)[:, 0, 0]
        order = np.argsort(luminance, kind="stable")
        for group in np.array_split(combined[order], min(max_samples, len(combined))):
            if not len(group):
                continue
            candidate = np.median(group, axis=0).astype(np.uint8)
            if not palette or all(
                np.linalg.norm(candidate.astype(float) - item.astype(float)) >= 3.0
                for item in palette
            ):
                palette.append(candidate)
    if not palette:
        palette.append(np.median(np.concatenate(regions, axis=0), axis=0).astype(np.uint8))
    return np.stack(palette[:max_samples], axis=0)


def _screen_sample_records(options: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate current location-aware screen samples."""

    records: list[dict[str, Any]] = []
    for sample in (options.get("screen_samples") or [])[:16]:
        if not isinstance(sample, dict):
            continue
        rgb = sample.get("rgb")
        if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
            continue
        records.append(
            {
                "rgb": tuple(int(np.clip(channel, 0, 255)) for channel in rgb),
                "x": sample.get("x"),
                "y": sample.get("y"),
                "source_timeline_ordinal": sample.get("source_timeline_ordinal"),
            }
        )
    return records[:16]


def _screen_palette_for_options(
    image: np.ndarray, options: dict[str, Any]
) -> np.ndarray:
    samples = _screen_sample_records(options)
    if samples:
        return np.array(
            [[record["rgb"][2], record["rgb"][1], record["rgb"][0]] for record in samples],
            dtype=np.uint8,
        )
    return estimate_background_palette_bgr(image)


def _screen_distance(
    image: np.ndarray, palette_bgr: np.ndarray, *, smooth: bool = True
) -> np.ndarray:
    """Return a perceptual backdrop distance on an approximately 0-100 scale.

    Chroma channels carry most of the weight so shadows and exposure gradients on
    a green screen remain keyable. A small luminance term still separates neutral
    objects with similar chroma. The closest of up to five samples wins.
    """

    image_ycc = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2YCrCb).astype(np.float32)
    distance = np.full(image.shape[:2], np.inf, dtype=np.float32)
    for sample in palette_bgr:
        sample_ycc = cv2.cvtColor(sample.reshape(1, 1, 3), cv2.COLOR_BGR2YCrCb).astype(np.float32)[0, 0]
        chroma = np.linalg.norm(image_ycc[:, :, 1:3] - sample_ycc[1:3], axis=2)
        luminance = np.abs(image_ycc[:, :, 0] - sample_ycc[0]) * 0.12
        distance = np.minimum(distance, chroma + luminance)
    return cv2.GaussianBlur(distance, (3, 3), 0.55) if smooth else distance


def _srgb_to_linear(values: np.ndarray) -> np.ndarray:
    if values.dtype == np.uint8:
        return cv2.LUT(values, _SRGB_TO_LINEAR_LUT)
    normalized = np.clip(values.astype(np.float32) / 255.0, 0.0, 1.0)
    return cv2.LUT(np.clip(normalized * 255.0, 0, 255).astype(np.uint8), _SRGB_TO_LINEAR_LUT)


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    linear = np.clip(values.astype(np.float32), 0.0, 1.0)
    encoded = np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    )
    return np.clip(encoded * 255.0, 0, 255).astype(np.uint8)


def _screen_chroma_direction(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return a per-pixel multi-channel screen direction and its peak.

    Subtracting the neutral component makes the same representation work for
    one-channel screens (green/blue/red) and two-channel screens
    (magenta/cyan/yellow).  Neutral screens intentionally have no direction.
    """

    source = values.astype(np.float32)
    neutral = np.min(source, axis=2, keepdims=True)
    chroma = np.maximum(source - neutral, 0.0)
    peak = np.max(chroma, axis=2, keepdims=True)
    direction = np.divide(
        chroma,
        np.maximum(peak, 1e-6),
        out=np.zeros_like(chroma),
        where=peak > 1e-6,
    )
    return direction.astype(np.float32), peak[:, :, 0].astype(np.float32)


def _screen_aligned_excess(
    values: np.ndarray, plate_values: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Measure removable screen-aligned colour without assuming one channel."""

    source = values.astype(np.float32)
    direction, direction_peak = _screen_chroma_direction(plate_values)
    non_screen = direction < 0.25
    baseline = np.max(
        np.where(non_screen, source, -1.0),
        axis=2,
    )
    has_non_screen = np.any(non_screen, axis=2)
    baseline = np.where(has_non_screen, np.maximum(baseline, 0.0), 0.0)
    per_channel = np.maximum(source - baseline[:, :, None], 0.0) * direction
    aligned = np.sum(per_channel, axis=2) / np.maximum(
        np.sum(direction, axis=2), 1e-6
    )
    aligned[direction_peak <= 1e-6] = 0.0
    return aligned.astype(np.float32), baseline.astype(np.float32), direction


def _spatial_screen_plate_bgr(image: np.ndarray, palette_bgr: np.ndarray) -> np.ndarray:
    """Build a smooth local screen reference instead of assuming one flat color."""

    height, width = image.shape[:2]
    if len(palette_bgr) == 1:
        return np.broadcast_to(palette_bgr[0], (height, width, 3)).copy()
    band_y = max(2, height // 24)
    band_x = max(2, width // 24)
    corners = [
        image[:band_y, :band_x, :3],
        image[:band_y, -band_x:, :3],
        image[-band_y:, :band_x, :3],
        image[-band_y:, -band_x:, :3],
    ]
    samples: list[np.ndarray] = []
    fallback = np.median(palette_bgr, axis=0)
    for corner in corners:
        preferred = _prefer_screen_pixels(corner.reshape(-1, 3))
        samples.append(np.median(preferred, axis=0) if len(preferred) else fallback)
    grid = np.array([[samples[0], samples[1]], [samples[2], samples[3]]], dtype=np.float32)
    return cv2.resize(grid, (width, height), interpolation=cv2.INTER_LINEAR).astype(np.uint8)


def _screen_unmix_alpha(
    image: np.ndarray,
    plate_bgr: np.ndarray,
    observed_linear: np.ndarray | None = None,
    plate_linear: np.ndarray | None = None,
) -> np.ndarray:
    """Estimate coverage by subtracting the locally estimated screen contribution."""

    observed = observed_linear if observed_linear is not None else _srgb_to_linear(image[:, :, :3])
    plate = plate_linear if plate_linear is not None else _srgb_to_linear(plate_bgr)
    observed_projection, _, direction = _screen_aligned_excess(
        observed, plate
    )
    plate_projection, _, _ = _screen_aligned_excess(plate, plate)
    direction_peak = np.max(direction, axis=2)
    background_fraction = np.divide(
        observed_projection,
        np.maximum(plate_projection, 1e-4),
        out=np.zeros_like(observed_projection),
        where=direction_peak > 1e-6,
    )
    background_fraction = np.clip(background_fraction, 0.0, 1.0)
    return np.clip((1.0 - background_fraction) * 255.0, 0, 255).astype(np.uint8)


def _recover_foreground_bgr(
    image: np.ndarray,
    alpha: np.ndarray,
    plate_bgr: np.ndarray,
    observed_linear: np.ndarray | None = None,
    plate_linear: np.ndarray | None = None,
) -> np.ndarray:
    result = image[:, :, :3].copy()
    edge = (alpha > 3) & (alpha < 252)
    result[alpha <= 3] = 0
    if not np.any(edge):
        return result
    observed = observed_linear if observed_linear is not None else _srgb_to_linear(image[:, :, :3])
    plate = plate_linear if plate_linear is not None else _srgb_to_linear(plate_bgr)
    coverage = alpha[edge].astype(np.float32)[:, None] / 255.0
    recovered = (observed[edge] - (1.0 - coverage) * plate[edge]) / np.maximum(
        coverage, 1.0 / 255.0
    )
    result[edge] = _linear_to_srgb(recovered)
    return result


def _screen_plate_for_options(image: np.ndarray, options: dict[str, Any]) -> np.ndarray:
    return _spatial_screen_plate_bgr(image, _screen_palette_for_options(image, options))


def _border_connected(candidate: np.ndarray) -> np.ndarray:
    binary = candidate.astype(np.uint8)
    count, labels = cv2.connectedComponents(binary, connectivity=8)
    if count <= 1:
        return np.zeros_like(candidate, dtype=bool)
    border_labels = np.unique(
        np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    )
    border_labels = border_labels[border_labels > 0]
    if not len(border_labels):
        return np.zeros_like(candidate, dtype=bool)
    return np.isin(labels, border_labels)


def _smoothstep(values: np.ndarray, low: float, high: float) -> np.ndarray:
    high = max(high, low + 1e-3)
    normalized = np.clip((values - low) / (high - low), 0.0, 1.0)
    return normalized * normalized * (3.0 - 2.0 * normalized)


def _clean_components(mask: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    binary = (mask > 24).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if count <= 1:
        return np.zeros_like(mask), {"components": 0, "secondary_ratio": 0.0}
    areas = stats[1:, cv2.CC_STAT_AREA]
    order = np.argsort(areas)[::-1]
    largest_area = int(areas[order[0]])
    secondary_area = int(areas[order[1]]) if len(order) > 1 else 0
    # Remove compression specks only. Detached weapons, sparks and spell effects are
    # valid animation content and must not be discarded just because they are not the
    # largest connected component.
    min_area = max(2, int(mask.size * 0.00002))
    valid_labels = [index + 1 for index, area in enumerate(areas) if int(area) >= min_area]
    kept = np.where(np.isin(labels, valid_labels), mask, 0).astype(np.uint8)
    return kept, {
        "components": int(count - 1),
        "largest_area": largest_area,
        "secondary_ratio": secondary_area / max(largest_area, 1),
    }


def _fill_small_holes(
    mask: np.ndarray,
    screen_lock: np.ndarray | None = None,
    max_area: int | None = None,
) -> np.ndarray:
    """Repair non-screen pinholes while preserving true backdrop gaps."""

    binary = np.where(mask > 127, 255, 0).astype(np.uint8)
    background = cv2.bitwise_not(binary)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(background, connectivity=8)
    if max_area is None:
        max_area = max(4, min(64, int(mask.size * 0.00005)))
    result = mask.copy()
    border_labels = set(
        int(value)
        for value in np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    )
    for label in range(1, count):
        if label in border_labels or int(stats[label, cv2.CC_STAT_AREA]) > max_area:
            continue
        hole = labels == label
        if screen_lock is not None and np.any(screen_lock[hole]):
            continue
        result[hole] = 255
    return result


def _despill(
    image: np.ndarray,
    alpha: np.ndarray,
    strength: float,
    plate_bgr: np.ndarray,
) -> np.ndarray:
    alpha_ratio = alpha.astype(np.float32) * (1.0 / 255.0)
    visible_edge_weight = np.where(alpha > 0, np.power(1.0 - alpha_ratio, 0.65), 0.0)
    return _conservative_manual_despill_bgr(
        image[:, :, :3],
        alpha,
        plate_bgr,
        np.clip(float(strength) * visible_edge_weight, 0.0, 1.0),
    )


def _bleed_foreground_rgb(
    image: np.ndarray, alpha: np.ndarray, iterations: int = 8
) -> np.ndarray:
    """Remove key color from transparent pixels and extend clean edge colors.

    Unity samples RGB even where alpha is zero. Keeping the original green screen
    there creates a visible fringe after bilinear filtering, so transparent RGB is
    rebuilt from nearby foreground colors instead of leaving key-green data behind.
    """
    result = image.copy()
    known = alpha > 16
    result[~known] = 0
    kernel = np.ones((3, 3), dtype=np.float32)
    for _ in range(max(0, iterations)):
        neighbor_count = cv2.filter2D(
            known.astype(np.float32),
            cv2.CV_32F,
            kernel,
            borderType=cv2.BORDER_CONSTANT,
        )
        expanded = neighbor_count > 0
        candidates = expanded & ~known
        if not np.any(candidates):
            break
        neighbor_sum = cv2.filter2D(
            result.astype(np.float32),
            cv2.CV_32F,
            kernel,
            borderType=cv2.BORDER_CONSTANT,
        )
        averaged = neighbor_sum / np.maximum(neighbor_count[:, :, None], 1.0)
        result[candidates] = np.clip(
            np.round(averaged[candidates]), 0, 255
        ).astype(np.uint8)
        known = expanded
    return result


def _screen_residue_ratio(
    rgb: np.ndarray,
    alpha: np.ndarray,
    plate_bgr: np.ndarray | None = None,
    screen_lock: np.ndarray | None = None,
) -> float:
    """Measure visible colour aligned with the configured local screen."""

    edge_pixels = (alpha > 16) & (alpha < 239)
    if plate_bgr is None:
        candidates = rgb[edge_pixels]
        if not len(candidates):
            return 0.0
        saturation = candidates.max(axis=1) - candidates.min(axis=1)
        candidates = candidates[saturation >= np.percentile(saturation, 60)]
        if not len(candidates):
            return 0.0
        inferred = np.median(candidates, axis=0).astype(np.uint8)
        plate_bgr = np.broadcast_to(inferred, rgb.shape).copy()
    elif plate_bgr.shape != rgb.shape:
        plate_bgr = cv2.resize(
            plate_bgr,
            (rgb.shape[1], rgb.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
    numerator, denominator = _screen_residue_accumulator(rgb, alpha, plate_bgr)
    edge_ratio = float(numerator / max(denominator, 1.0))
    if screen_lock is None:
        return edge_ratio
    visible = alpha > 16
    opaque_screen_ratio = float(
        np.count_nonzero(screen_lock & (alpha >= 239)) / max(np.count_nonzero(visible), 1)
    )
    return max(edge_ratio, opaque_screen_ratio)


def _screen_residue_accumulator(
    rgb: np.ndarray,
    alpha: np.ndarray,
    plate_bgr: np.ndarray,
) -> tuple[float, float]:
    """Return additive residue terms used for exact ROI QC updates."""

    remaining_screen, _, _ = _screen_aligned_excess(rgb, plate_bgr)
    edge_pixels = (alpha > 16) & (alpha < 239)
    edge_visibility = np.where(
        edge_pixels, alpha.astype(np.float32) / 255.0, 0.0
    )
    return (
        float(np.sum(remaining_screen * edge_visibility)),
        255.0 * float(np.sum(edge_visibility)),
    )


def _key_mode(options: dict[str, Any]) -> str:
    mode = options.get("key_mode", "clean_screen")
    if mode in {"clean_screen", "preserve_subject_screen_color"}:
        return str(mode)
    raise ValueError(f"不支持的 key_mode：{mode!r}")


def _screen_lock_for_options(image: np.ndarray, options: dict[str, Any]) -> np.ndarray:
    palette = _screen_palette_for_options(image, options)
    return _screen_distance(image, palette, smooth=False) <= float(
        options.get("threshold_low", 18.0)
    )


def _compose_straight_alpha_rgba(
    image: np.ndarray,
    alpha: np.ndarray,
    spill_strength: float,
    plate_bgr: np.ndarray | None = None,
    observed_linear: np.ndarray | None = None,
    plate_linear: np.ndarray | None = None,
    apply_despill: bool = True,
    bleed_iterations: int = 8,
) -> np.ndarray:
    base = (
        _recover_foreground_bgr(
            image, alpha, plate_bgr, observed_linear, plate_linear
        )
        if plate_bgr is not None
        else image[:, :, :3]
    )
    corrected = (
        _despill(base, alpha, spill_strength, plate_bgr)
        if apply_despill and plate_bgr is not None
        else base
    )
    corrected = _bleed_foreground_rgb(corrected, alpha, iterations=bleed_iterations)
    rgba = cv2.cvtColor(corrected, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = alpha
    return rgba


CONSTRAINT_FOREGROUND = 0
CONSTRAINT_BACKGROUND = 1
CONSTRAINT_DESPILL = 2


def _conservative_manual_despill_bgr(
    image: np.ndarray,
    alpha: np.ndarray,
    plate_bgr: np.ndarray,
    coverage: np.ndarray,
    *,
    dominant_channel: int | None = None,
    observed_bgr: np.ndarray | None = None,
    subject_core: np.ndarray | None = None,
) -> np.ndarray:
    """Apply one-way multi-channel screen cleanup to straight BGR.

    Alpha is immutable.  A pure screen pixel has no non-screen colour support
    and remains unchanged so background removal stays owned by the background
    brush.  When the original composite is available, a physically feasible
    fixed-alpha unmix supplies the preferred target; otherwise the conservative
    screen-vector projection is used.
    """

    if (
        image.ndim != 3
        or image.shape[2] != 3
        or alpha.shape != image.shape[:2]
        or plate_bgr.shape != image.shape
        or coverage.shape != image.shape[:2]
        or (subject_core is not None and subject_core.shape != image.shape[:2])
    ):
        raise ValueError("人工去幕色输入尺寸不一致。")
    source = image.astype(np.float32)
    plate = plate_bgr.astype(np.float32)
    if dominant_channel is None:
        _, baseline, screen_direction = _screen_aligned_excess(source, plate)
        _, direction_peak = _screen_chroma_direction(plate)
    else:
        if dominant_channel < 0 or dominant_channel > 2:
            raise ValueError("人工去幕色主通道无效。")
        screen_direction = np.zeros_like(source, dtype=np.float32)
        screen_direction[:, :, dominant_channel] = 1.0
        direction_peak = np.ones(image.shape[:2], dtype=np.float32)
        other = [channel for channel in range(3) if channel != dominant_channel]
        baseline = np.maximum(source[:, :, other[0]], source[:, :, other[1]])
    full_reduction = (
        np.maximum(source - baseline[:, :, None], 0.0) * screen_direction
    )
    projected_target = np.clip(source - full_reduction, 0.0, 255.0).astype(
        np.uint8
    )
    source_linear = _srgb_to_linear(image)
    protected = (
        np.zeros(image.shape[:2], dtype=bool)
        if subject_core is None
        else subject_core.astype(bool)
    )
    if observed_bgr is None:
        result_f32 = source + (projected_target.astype(np.float32) - source) * (
            np.clip(coverage.astype(np.float32), 0.0, 1.0)
            * np.clip((baseline - 8.0) / 48.0, 0.0, 1.0)
            * (alpha > 2).astype(np.float32)
            * (direction_peak > 1e-5).astype(np.float32)
            * (~protected).astype(np.float32)
        )[:, :, None]
        changed = result_f32 < source - 1e-6
        result = image.copy()
        rounded = np.clip(np.floor(result_f32 + 0.5), 0, 255).astype(np.uint8)
        result[changed] = rounded[changed]
        return result

    target_linear = _srgb_to_linear(projected_target)
    if observed_bgr is not None:
        if observed_bgr.shape != image.shape:
            raise ValueError("人工去幕色原始合成图尺寸不一致。")
        observed_linear = _srgb_to_linear(observed_bgr)
        plate_linear = _srgb_to_linear(plate_bgr)
        alpha_float = alpha.astype(np.float32) / 255.0
        raw = observed_linear - (1.0 - alpha_float[:, :, None]) * plate_linear
        feasible = (
            (alpha_float > (2.0 / 255.0))
            & np.all(raw >= -1e-4, axis=2)
            & np.all(raw <= alpha_float[:, :, None] + 1e-4, axis=2)
        )
        straight = np.divide(
            np.clip(raw, 0.0, alpha_float[:, :, None]),
            np.maximum(alpha_float[:, :, None], 1.0 / 255.0),
            out=np.zeros_like(raw),
            where=alpha_float[:, :, None] > (1.0 / 255.0),
        )
        physical_target = source_linear - (
            np.maximum(source_linear - straight, 0.0) * screen_direction
        )
        target_linear = np.where(
            feasible[:, :, None],
            np.minimum(source_linear, physical_target),
            target_linear,
        )
    # Match the canvas preview: pure screen color remains untouched, while a
    # small but real non-screen component progressively authorizes cleanup.
    support = np.clip((baseline - 8.0) / 48.0, 0.0, 1.0)
    weight = (
        np.clip(coverage.astype(np.float32), 0.0, 1.0)
        * support
        * (alpha > 2).astype(np.float32)
        * (direction_peak > 1e-5).astype(np.float32)
        * (~protected).astype(np.float32)
    )
    result_linear = source_linear + (
        np.minimum(source_linear, target_linear) - source_linear
    ) * weight[:, :, None]
    encoded = _linear_to_srgb(result_linear)
    reduction = (source_linear - result_linear) > 1e-7
    result = image.copy()
    result[reduction] = np.minimum(encoded[reduction], image[reduction])
    return result


def fit_screen_model(
    image: np.ndarray,
    options: dict[str, Any],
    ai_alpha: np.ndarray | None = None,
    constraints: np.ndarray | None = None,
    source_timeline_ordinal: int | None = None,
) -> dict[str, Any]:
    """Fit a low-frequency per-frame screen plate from reliable screen pixels."""

    height, width = image.shape[:2]
    rows = int(np.clip(math.ceil(height / 96.0), 8, 24))
    cols = int(np.clip(math.ceil(width / 96.0), 8, 24))
    palette = _screen_palette_for_options(image, options)
    distance = _screen_distance(image, palette, smooth=False)
    low = float(options.get("threshold_low", 18.0))
    high = max(low + 1.0, float(options.get("threshold_high", 62.0)))
    candidate = _border_connected(distance <= max(high * 1.35, low + 12.0))
    if ai_alpha is not None:
        normalized_ai = ai_alpha
        if normalized_ai.shape != (height, width):
            normalized_ai = cv2.resize(
                normalized_ai, (width, height), interpolation=cv2.INTER_LINEAR
            )
        candidate &= normalized_ai <= 40
    if constraints is not None and constraints.shape[:2] == (height, width):
        candidate |= constraints[:, :, CONSTRAINT_BACKGROUND] >= 192

    fallback_bgr = np.median(palette.astype(np.float32), axis=0)
    fallback = _srgb_to_linear(
        np.clip(fallback_bgr, 0, 255).astype(np.uint8).reshape(1, 1, 3)
    )[0, 0]
    grid = np.full((rows, cols, 3), np.nan, dtype=np.float32)
    confidence = np.zeros((rows, cols), dtype=np.float32)
    for row in range(rows):
        y0 = round(row * height / rows)
        y1 = round((row + 1) * height / rows)
        for col in range(cols):
            x0 = round(col * width / cols)
            x1 = round((col + 1) * width / cols)
            local_mask = candidate[y0:y1, x0:x1]
            local = image[y0:y1, x0:x1, :3][local_mask]
            cell_area = max((y1 - y0) * (x1 - x0), 1)
            if len(local) < max(12, int(cell_area * 0.025)):
                continue
            values = _srgb_to_linear(local.reshape(-1, 1, 3)).reshape(-1, 3)
            median = np.median(values, axis=0)
            mad = np.median(np.abs(values - median), axis=0)
            limit = np.maximum(0.008, mad * 3.5)
            kept = values[np.all(np.abs(values - median) <= limit, axis=1)]
            if len(kept) >= 8:
                values = kept
                median = np.median(values, axis=0)
            residual = float(np.mean(np.linalg.norm(values - median, axis=1)))
            support = min(1.0, len(values) / max(cell_area * 0.22, 1.0))
            grid[row, col] = median
            confidence[row, col] = support * math.exp(-residual / 0.12)

    # Location-aware samples pin the corresponding grid cell. Samples from a
    # different retained frame still contribute to the global palette above,
    # but do not impose a spatial value on this frame.
    anchored: dict[tuple[int, int], list[np.ndarray]] = {}
    for record in _screen_sample_records(options):
        ordinal = record.get("source_timeline_ordinal")
        if ordinal is not None and source_timeline_ordinal is not None and int(ordinal) != int(source_timeline_ordinal):
            continue
        x = record.get("x")
        y = record.get("y")
        if x is None or y is None:
            continue
        row = int(np.clip(float(y), 0.0, 1.0) * (rows - 1))
        col = int(np.clip(float(x), 0.0, 1.0) * (cols - 1))
        rgb = record["rgb"]
        sample_bgr = np.array([rgb[2], rgb[1], rgb[0]], dtype=np.uint8)
        anchored.setdefault((row, col), []).append(
            _srgb_to_linear(sample_bgr.reshape(1, 1, 3))[0, 0]
        )
    for (row, col), samples in anchored.items():
        grid[row, col] = np.median(np.stack(samples), axis=0)
        confidence[row, col] = 1.0

    known = np.isfinite(grid[:, :, 0])
    if not np.any(known):
        grid[:] = fallback
        confidence[:] = 0.05
    else:
        # Fill unsupported cells from adjacent supported cells. Keeping this on
        # the tiny grid makes the extrapolation deterministic and inexpensive.
        for _ in range(rows + cols):
            missing = ~np.isfinite(grid[:, :, 0])
            if not np.any(missing):
                break
            next_grid = grid.copy()
            next_confidence = confidence.copy()
            changed = False
            for row, col in zip(*np.where(missing), strict=False):
                neighbors: list[np.ndarray] = []
                neighbor_confidence: list[float] = []
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    yy, xx = row + dy, col + dx
                    if 0 <= yy < rows and 0 <= xx < cols and np.isfinite(grid[yy, xx, 0]):
                        neighbors.append(grid[yy, xx])
                        neighbor_confidence.append(float(confidence[yy, xx]))
                if neighbors:
                    next_grid[row, col] = np.mean(np.stack(neighbors), axis=0)
                    next_confidence[row, col] = max(0.05, np.mean(neighbor_confidence) * 0.72)
                    changed = True
            grid, confidence = next_grid, next_confidence
            if not changed:
                break
        missing = ~np.isfinite(grid[:, :, 0])
        grid[missing] = fallback
        confidence[missing] = 0.05

    median_linear = np.median(grid.reshape(-1, 3), axis=0)
    grid_bgr = _linear_to_srgb(np.clip(grid, 0, 1))
    median_bgr = _linear_to_srgb(
        np.clip(median_linear, 0, 1).reshape(1, 1, 3)
    )[0, 0]
    return {
        "grid_linear": np.clip(grid, 0, 1).astype(np.float32),
        "grid_bgr": grid_bgr.astype(np.float32),
        "grid_confidence": np.clip(confidence, 0, 1).astype(np.float32),
        "median_bgr": median_bgr.astype(np.float32),
        "luminance": float(
            cv2.cvtColor(median_bgr.reshape(1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2GRAY)[0, 0]
        ),
        "candidate_ratio": float(np.count_nonzero(candidate) / max(candidate.size, 1)),
        "mean_confidence": float(np.mean(confidence)),
    }


def _screen_models_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_color = left["median_bgr"].astype(np.float32)
    right_color = right["median_bgr"].astype(np.float32)
    color_jump = float(np.linalg.norm(left_color - right_color) / 441.7)
    left_luma = float(left["luminance"])
    right_luma = float(right["luminance"])
    luma_jump = abs(left_luma - right_luma) / max(left_luma, 24.0)
    if color_jump > 0.12 or luma_jump > 0.28:
        return False
    left_confidence = left["grid_confidence"].astype(np.float32)
    right_confidence = right["grid_confidence"].astype(np.float32)
    mutual = (left_confidence >= 0.18) & (right_confidence >= 0.18)
    if np.count_nonzero(mutual) < 4:
        return True
    left_grid = (
        left.get("grid_linear")
        if left.get("grid_linear") is not None
        else _srgb_to_linear(np.clip(left["grid_bgr"], 0, 255).astype(np.uint8))
    )
    right_grid = (
        right.get("grid_linear")
        if right.get("grid_linear") is not None
        else _srgb_to_linear(np.clip(right["grid_bgr"], 0, 255).astype(np.uint8))
    )
    grid_jump = float(
        np.median(np.linalg.norm(left_grid[mutual] - right_grid[mutual], axis=1))
    )
    return grid_jump <= 0.16


def stabilize_screen_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a robust sequence clean plate without crossing exposure cuts.

    Each frame still owns a local low-frequency plate. Compatible consecutive
    frames contribute their high-confidence cells to a sequence consensus, so
    positions hidden by the moving subject can be recovered from another frame.
    """

    if len(models) < 2:
        return models
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for model in models:
        if current and (
            not _screen_models_compatible(current[-1], model)
            or not _screen_models_compatible(current[0], model)
        ):
            groups.append(current)
            current = []
        current.append(model)
    if current:
        groups.append(current)

    stabilized: list[dict[str, Any]] = []
    for group in groups:
        linear_stack = np.stack(
            [
                item.get("grid_linear")
                if item.get("grid_linear") is not None
                else _srgb_to_linear(
                    np.clip(item["grid_bgr"], 0, 255).astype(np.uint8)
                )
                for item in group
            ]
        ).astype(np.float32)
        confidence_stack = np.stack(
            [item["grid_confidence"] for item in group]
        ).astype(np.float32)
        reliable = confidence_stack >= 0.18
        support_count = np.count_nonzero(reliable, axis=0).astype(np.float32)
        masked = np.where(
            reliable[:, :, :, None], linear_stack, np.nan
        )
        no_support = support_count == 0
        if np.any(no_support):
            masked[0][no_support] = linear_stack[0][no_support]
        with np.errstate(invalid="ignore"):
            sequence_grid = np.nanmedian(masked, axis=0)
        sequence_grid = np.where(
            np.isfinite(sequence_grid), sequence_grid, linear_stack[0]
        ).astype(np.float32)
        reliable_confidence = np.where(
            reliable,
            np.clip(confidence_stack * 0.72, 0.0, 0.95),
            0.0,
        )
        sequence_confidence = np.where(
            support_count > 0,
            1.0 - np.prod(1.0 - reliable_confidence, axis=0),
            0.0,
        ).astype(np.float32)
        for model in group:
            updated = dict(model)
            local_grid = (
                model.get("grid_linear")
                if model.get("grid_linear") is not None
                else _srgb_to_linear(
                    np.clip(model["grid_bgr"], 0, 255).astype(np.uint8)
                )
            ).astype(np.float32)
            local_confidence = np.clip(
                model["grid_confidence"].astype(np.float32), 0.0, 1.0
            )
            local_weight = local_confidence[:, :, None]
            sequence_weight = (sequence_confidence * 0.92)[:, :, None]
            combined_grid = (
                local_grid * local_weight + sequence_grid * sequence_weight
            ) / np.maximum(local_weight + sequence_weight, 1e-4)
            combined_confidence = 1.0 - (
                1.0 - local_confidence
            ) * (1.0 - sequence_confidence * 0.92)
            updated["grid_linear"] = np.clip(combined_grid, 0, 1).astype(np.float32)
            updated["grid_bgr"] = _linear_to_srgb(updated["grid_linear"]).astype(
                np.float32
            )
            updated["grid_confidence"] = np.clip(
                combined_confidence, 0, 1
            ).astype(np.float32)
            median_linear = np.median(
                updated["grid_linear"].reshape(-1, 3), axis=0
            )
            updated["median_bgr"] = _linear_to_srgb(
                median_linear.reshape(1, 1, 3)
            )[0, 0].astype(np.float32)
            updated["luminance"] = float(
                cv2.cvtColor(
                    updated["median_bgr"].reshape(1, 1, 3).astype(np.uint8),
                    cv2.COLOR_BGR2GRAY,
                )[0, 0]
            )
            updated["mean_confidence"] = float(
                np.mean(updated["grid_confidence"])
            )
            updated["sequence_plate_frames"] = len(group)
            updated["sequence_plate_support"] = float(np.mean(support_count))
            updated["sequence_plate_confidence"] = float(
                np.mean(sequence_confidence)
            )
            stabilized.append(updated)
    return stabilized


def _materialize_screen_model(
    model: dict[str, Any], width: int, height: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    grid_linear = model.get("grid_linear")
    if grid_linear is None:
        grid_linear = _srgb_to_linear(
            np.clip(model["grid_bgr"], 0, 255).astype(np.uint8)
        )
    plate_linear = cv2.resize(
        grid_linear, (width, height), interpolation=cv2.INTER_CUBIC
    )
    confidence = cv2.resize(
        model["grid_confidence"], (width, height), interpolation=cv2.INTER_LINEAR
    )
    plate_linear = np.clip(plate_linear, 0, 1).astype(np.float32)
    return (
        _linear_to_srgb(plate_linear),
        plate_linear,
        np.clip(confidence, 0, 1).astype(np.float32),
    )


def screen_plate_for_qc(
    shape: tuple[int, int],
    options: dict[str, Any],
    *,
    source_bgr: np.ndarray | None = None,
    screen_model: dict[str, Any] | None = None,
) -> np.ndarray:
    """Resolve the authoritative local screen plate for external-RGBA QC."""

    height, width = shape
    if height < 1 or width < 1:
        raise ValueError("幕色 QC 尺寸无效。")
    model = screen_model
    if model is not None and not isinstance(model, dict):
        raise ValueError("帧幕色模型投影无效，不能执行残留 QC。")
    if model is not None and "grid_bgr" not in model:
        rows = int(model.get("rows") or 0)
        cols = int(model.get("cols") or 0)
        rgb = np.asarray(model.get("rgb") or [], dtype=np.float32)
        confidence = np.asarray(model.get("confidence") or [], dtype=np.float32)
        if (
            int(model.get("revision") or 0) != CHROMA_COLOR_RECOVERY_REVISION
            or rows < 1
            or cols < 1
            or rgb.shape != (rows * cols, 3)
            or confidence.shape != (rows * cols,)
        ):
            raise ValueError("帧幕色模型投影无效，不能执行残留 QC。")
        model = {
            "grid_bgr": rgb.reshape(rows, cols, 3)[:, :, ::-1],
            "grid_confidence": np.clip(
                confidence.reshape(rows, cols) / 255.0, 0.0, 1.0
            ).astype(np.float32),
        }
    if model is None:
        if (
            source_bgr is None
            or source_bgr.ndim != 3
            or source_bgr.shape != (height, width, 3)
        ):
            raise ValueError("缺少可用于幕色残留 QC 的原始帧。")
        model = fit_screen_model(source_bgr, options)
    plate_bgr, _, _ = _materialize_screen_model(model, width, height)
    return plate_bgr


def _materialize_screen_model_roi(
    model: dict[str, Any],
    width: int,
    height: int,
    bbox: tuple[int, int, int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Materialize only a requested screen-model rectangle."""

    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("幕布模型 ROI 无效。")
    grid_linear = model.get("grid_linear")
    if grid_linear is None:
        grid_linear = _srgb_to_linear(
            np.clip(model["grid_bgr"], 0, 255).astype(np.uint8)
        )
    grid_linear = np.asarray(grid_linear, dtype=np.float32)
    grid_confidence = np.asarray(model["grid_confidence"], dtype=np.float32)
    rows, cols = grid_linear.shape[:2]
    xs = (
        (np.arange(x0, x1, dtype=np.float32) + 0.5) * cols / max(width, 1)
        - 0.5
    )
    ys = (
        (np.arange(y0, y1, dtype=np.float32) + 0.5) * rows / max(height, 1)
        - 0.5
    )
    map_x, map_y = np.meshgrid(xs, ys)
    plate_linear = cv2.remap(
        grid_linear,
        map_x,
        map_y,
        interpolation=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    confidence = cv2.remap(
        grid_confidence,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    plate_linear = np.clip(plate_linear, 0, 1).astype(np.float32)
    return (
        _linear_to_srgb(plate_linear),
        plate_linear,
        np.clip(confidence, 0, 1).astype(np.float32),
    )


def _local_screen_distance(image: np.ndarray, plate_bgr: np.ndarray) -> np.ndarray:
    observed = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2YCrCb).astype(np.float32)
    plate = cv2.cvtColor(plate_bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    chroma = np.linalg.norm(observed[:, :, 1:3] - plate[:, :, 1:3], axis=2)
    luminance = np.abs(observed[:, :, 0] - plate[:, :, 0]) * 0.12
    return cv2.GaussianBlur(chroma + luminance, (3, 3), 0.55)


def apply_clean_plate_prior(
    image: np.ndarray,
    current_model: dict[str, Any],
    reference_image: np.ndarray,
    reference_model: dict[str, Any],
    options: dict[str, Any],
    *,
    current_alpha: np.ndarray | None = None,
    reference_alpha: np.ndarray | None = None,
    reference_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt a validated Clean Plate in linear RGB without blind subtraction."""

    if reference_image.shape[:2] != image.shape[:2]:
        raise RuntimeError("Clean Plate 与正式帧尺寸不一致。")
    height, width = image.shape[:2]
    current_plate, _, current_confidence = _materialize_screen_model(
        current_model, width, height
    )
    reference_plate, reference_plate_linear, reference_confidence = (
        _materialize_screen_model(reference_model, width, height)
    )
    low = float(options.get("threshold_low", 18.0))
    high = max(low + 1.0, float(options.get("threshold_high", 62.0)))
    current_reliable = (
        (_local_screen_distance(image, current_plate) <= max(low * 1.35, low + 4.0))
        & (current_confidence >= 0.18)
    )
    reference_reliable = (
        (_local_screen_distance(reference_image, reference_plate) <= high)
        & (reference_confidence >= 0.18)
    )

    def exclude_subject(mask: np.ndarray, alpha: np.ndarray | None) -> np.ndarray:
        if alpha is None:
            return mask
        normalized = alpha
        if normalized.shape != (height, width):
            normalized = cv2.resize(
                normalized, (width, height), interpolation=cv2.INTER_LINEAR
            )
        divisor = 255.0 if normalized.dtype == np.uint8 else 1.0
        return mask & (normalized.astype(np.float32) / divisor <= 0.12)

    reliable = exclude_subject(current_reliable, current_alpha)
    reliable &= exclude_subject(reference_reliable, reference_alpha)
    reliable_count = int(np.count_nonzero(reliable))
    reliable_ratio = reliable_count / max(reliable.size, 1)
    if reliable_count < 256 or reliable_ratio < 0.02:
        raise RuntimeError("Clean Plate 有效幕布覆盖不足，无法安全适配曝光。")
    current_linear = _srgb_to_linear(image[:, :, :3])
    reference_linear = _srgb_to_linear(reference_image[:, :, :3])
    log_ratio = np.log(
        np.maximum(current_linear[reliable], 1.0 / 4096.0)
    ) - np.log(np.maximum(reference_linear[reliable], 1.0 / 4096.0))
    median_log_gain = np.median(log_ratio, axis=0)
    residual = np.median(np.abs(log_ratio - median_log_gain), axis=0)
    gain = np.exp(median_log_gain)
    if (
        np.any(gain < 0.50)
        or np.any(gain > 2.0)
        or float(np.max(residual)) > 0.22
    ):
        raise RuntimeError("Clean Plate 与当前帧属于不兼容的曝光段。")
    adapted_full = np.clip(
        reference_plate_linear * gain.reshape(1, 1, 3), 0, 1
    )
    grid_shape = current_model["grid_linear"].shape[:2]
    adapted_grid = cv2.resize(
        adapted_full,
        (grid_shape[1], grid_shape[0]),
        interpolation=cv2.INTER_AREA,
    ).astype(np.float32)
    reference_grid_confidence = cv2.resize(
        reference_model["grid_confidence"].astype(np.float32),
        (grid_shape[1], grid_shape[0]),
        interpolation=cv2.INTER_LINEAR,
    )
    adapted_grid_bgr = _linear_to_srgb(adapted_grid).astype(np.float32)
    median_bgr = np.median(adapted_grid_bgr.reshape(-1, 3), axis=0)
    result = dict(current_model)
    result.update(
        grid_linear=adapted_grid,
        grid_bgr=adapted_grid_bgr,
        grid_confidence=np.maximum(
            current_model["grid_confidence"].astype(np.float32) * 0.35,
            np.clip(reference_grid_confidence, 0.0, 1.0) * 0.92,
        ).astype(np.float32),
        median_bgr=median_bgr.astype(np.float32),
        luminance=float(
            cv2.cvtColor(
                median_bgr.reshape(1, 1, 3).astype(np.uint8),
                cv2.COLOR_BGR2GRAY,
            )[0, 0]
        ),
        clean_plate_mode=(reference_metadata or {}).get("mode"),
        clean_plate_sha256=(reference_metadata or {}).get("sha256"),
        clean_plate_reliable_ratio=float(reliable_ratio),
        clean_plate_exposure_gain=[float(value) for value in gain],
    )
    return result


def _constraint_weights(
    constraints: np.ndarray | None, shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if constraints is None or constraints.shape[:2] != shape or constraints.ndim != 3 or constraints.shape[2] != 3:
        empty = np.zeros(shape, dtype=np.float32)
        return empty, empty.copy(), empty.copy()
    values = constraints.astype(np.float32) / 255.0
    return (
        values[:, :, CONSTRAINT_FOREGROUND],
        values[:, :, CONSTRAINT_BACKGROUND],
        values[:, :, CONSTRAINT_DESPILL],
    )


def _primary_semantic_region(ai: np.ndarray) -> np.ndarray:
    """Return the single dominant semantic subject region for this character frame."""

    support = (ai >= 0.48).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        support, connectivity=8
    )
    if count <= 1:
        return np.zeros_like(ai, dtype=bool)
    scores = [
        float(np.sum(ai[labels == label]))
        for label in range(1, count)
    ]
    primary_label = int(np.argmax(scores)) + 1
    if int(stats[primary_label, cv2.CC_STAT_AREA]) < max(8, int(ai.size * 0.0005)):
        return np.zeros_like(ai, dtype=bool)
    return labels == primary_label


def _connected_semantic_core(ai: np.ndarray) -> np.ndarray:
    """Keep high-confidence core pixels only inside the dominant subject."""

    return (ai >= 0.97) & _primary_semantic_region(ai)


def _non_screen_color_support(
    image: np.ndarray, plate_bgr: np.ndarray
) -> np.ndarray:
    """Measure positive colour residual outside the screen-dominant channel."""

    plate = plate_bgr.astype(np.float32)
    source = image[:, :, :3].astype(np.float32)
    neutral = np.min(plate, axis=2, keepdims=True)
    dominant = np.argmax(np.maximum(plate - neutral, 0.0), axis=2)
    channel_indices = np.arange(3, dtype=np.int32).reshape(1, 1, 3)
    positive_residual = np.maximum(source - plate, 0.0)
    other_residual = np.max(
        np.where(
            channel_indices != dominant[:, :, None], positive_residual, 0.0
        ),
        axis=2,
    )
    return _smoothstep(other_residual, 6.0, 72.0).astype(np.float32)


def _suppress_screen_like_islands(
    alpha: np.ndarray,
    screen_similarity: np.ndarray,
    non_screen_support: np.ndarray,
    primary_semantic: np.ndarray,
    manual_foreground: np.ndarray,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Remove detached screen-colour islands while preserving real effects."""

    visible = (alpha > (16.0 / 255.0)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        visible, connectivity=8
    )
    if count <= 1:
        return alpha, {
            "garbage_components_removed": 0,
            "garbage_removed_ratio": 0.0,
            "screen_island_candidates": 0,
        }
    if np.any(primary_semantic):
        anchor = cv2.dilate(
            primary_semantic.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
        ).astype(bool)
    else:
        largest_label = int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) + 1
        anchor = labels == largest_label
    result = alpha.copy()
    removed_pixels = 0
    removed_components = 0
    candidates = 0
    for label in range(1, count):
        component = labels == label
        if np.any(anchor & component) or np.any(manual_foreground & component):
            continue
        candidates += 1
        screen_ratio = float(np.mean(screen_similarity[component] >= 0.48))
        foreground_color = float(np.mean(non_screen_support[component]))
        if screen_ratio < 0.62 or foreground_color > 0.20:
            continue
        removed_pixels += int(np.count_nonzero(component))
        removed_components += 1
        result[component] = 0.0
    return result, {
        "garbage_components_removed": removed_components,
        "garbage_removed_ratio": float(removed_pixels / max(alpha.size, 1)),
        "screen_island_candidates": candidates,
    }


def _solve_edge_aware_alpha(
    guide_bgr: np.ndarray,
    prior: np.ndarray,
    data_weight: np.ndarray,
    foreground_seed: np.ndarray,
    background_seed: np.ndarray,
) -> np.ndarray:
    """Solve a bounded edge-aware alpha field with a small multiscale Jacobi pass."""

    height, width = prior.shape
    scales = [scale for scale in (0.25, 0.5, 1.0) if min(height, width) * scale >= 24]
    if not scales or scales[-1] != 1.0:
        scales.append(1.0)
    alpha: np.ndarray | None = None
    for scale in scales:
        target_width = max(1, round(width * scale))
        target_height = max(1, round(height * scale))
        target_size = (target_width, target_height)
        local_prior = cv2.resize(prior, target_size, interpolation=cv2.INTER_LINEAR)
        local_weight = cv2.resize(data_weight, target_size, interpolation=cv2.INTER_LINEAR)
        local_guide = cv2.resize(guide_bgr, target_size, interpolation=cv2.INTER_AREA)
        local_fg = cv2.resize(
            foreground_seed.astype(np.uint8), target_size, interpolation=cv2.INTER_NEAREST
        ).astype(bool)
        local_bg = cv2.resize(
            background_seed.astype(np.uint8), target_size, interpolation=cv2.INTER_NEAREST
        ).astype(bool)
        alpha = (
            cv2.resize(alpha, target_size, interpolation=cv2.INTER_LINEAR)
            if alpha is not None
            else local_prior.copy()
        )
        guide_linear = _srgb_to_linear(local_guide)
        horizontal = np.exp(
            -np.sum(np.square(guide_linear[:, 1:] - guide_linear[:, :-1]), axis=2) / 0.018
        ).astype(np.float32)
        vertical = np.exp(
            -np.sum(np.square(guide_linear[1:, :] - guide_linear[:-1, :]), axis=2) / 0.018
        ).astype(np.float32)
        smoothness = 0.34
        iterations = 10 if scale < 1.0 else 8
        for _ in range(iterations):
            numerator = local_weight * local_prior
            denominator = local_weight.copy()
            numerator[:, 1:] += smoothness * horizontal * alpha[:, :-1]
            denominator[:, 1:] += smoothness * horizontal
            numerator[:, :-1] += smoothness * horizontal * alpha[:, 1:]
            denominator[:, :-1] += smoothness * horizontal
            numerator[1:, :] += smoothness * vertical * alpha[:-1, :]
            denominator[1:, :] += smoothness * vertical
            numerator[:-1, :] += smoothness * vertical * alpha[1:, :]
            denominator[:-1, :] += smoothness * vertical
            solved = numerator / np.maximum(denominator, 1e-4)
            alpha = alpha * 0.28 + solved * 0.72
            alpha[local_fg] = 1.0
            alpha[local_bg] = 0.0
        alpha = np.clip(alpha, 0, 1)
    assert alpha is not None
    return alpha.astype(np.float32)


def _physical_alpha_floor(
    observed_linear: np.ndarray, plate_linear: np.ndarray
) -> np.ndarray:
    brighter = np.maximum(
        (observed_linear - plate_linear) / np.maximum(1.0 - plate_linear, 1e-4),
        0.0,
    )
    darker = np.maximum(
        (plate_linear - observed_linear) / np.maximum(plate_linear, 1e-4),
        0.0,
    )
    return np.clip(np.max(np.maximum(brighter, darker), axis=2), 0, 1).astype(np.float32)


def _recover_premultiplied_linear(
    image: np.ndarray,
    alpha: np.ndarray,
    plate_bgr: np.ndarray,
    spill_strength: float,
    despill_weight: np.ndarray,
    screen_confidence: np.ndarray,
    screen_similarity: np.ndarray,
    semantic_foreground: np.ndarray,
    protect_subject_screen_color: bool,
    fallback_rgba: np.ndarray | None = None,
    *,
    observed_linear_bgr: np.ndarray | None = None,
) -> tuple[np.ndarray, float, float]:
    observed = (
        np.asarray(observed_linear_bgr, dtype=np.float32)
        if observed_linear_bgr is not None
        else _srgb_to_linear(image[:, :, :3])
    )
    if observed.shape != image[:, :, :3].shape or not np.isfinite(observed).all():
        raise RuntimeError("原始线性 RGB 与抠图帧尺寸不一致或包含无效数值。")
    plate = _srgb_to_linear(plate_bgr)
    raw = observed - (1.0 - alpha[:, :, None]) * plate
    alpha3 = alpha[:, :, None]
    clipping = (raw < -1e-4) | (raw > alpha3 + 1e-4)
    visible = alpha > (1.0 / 255.0)
    clipping_ratio = float(
        np.count_nonzero(clipping & visible[:, :, None])
        / max(np.count_nonzero(visible) * 3, 1)
    )
    immutable_source = np.clip(observed * alpha3, 0.0, alpha3)
    stable_fallback = immutable_source
    if fallback_rgba is not None and fallback_rgba.shape[:2] == alpha.shape:
        stable_fallback = np.clip(
            _srgb_to_linear(fallback_rgba[:, :, :3]) * alpha3,
            0.0,
            alpha3,
        )

    # Resolve the chromatic direction of the screen once and keep neutral
    # screens on the conservative path.  On a green screen the red/blue
    # channels contain much less screen energy, so their physical unmixing is
    # materially better conditioned than the green channel.
    neutral_plate = np.min(plate, axis=2, keepdims=True)
    screen_direction = np.maximum(plate - neutral_plate, 0.0)
    direction_peak = np.max(screen_direction, axis=2, keepdims=True)
    screen_direction = np.divide(
        screen_direction,
        np.maximum(direction_peak, 1e-5),
        out=np.zeros_like(screen_direction),
        where=direction_peak > 1e-5,
    )
    direction_present = (direction_peak > 1e-5).astype(np.float32)

    # A single composite does not prove that every soft pixel contains screen
    # light. The screen-aligned channel therefore keeps the strict similarity
    # gate, while better-conditioned non-screen channels may use the fitted
    # screen confidence directly. Confident subject RGB stays byte-for-byte
    # close to the immutable source.
    soft_coverage = (
        _smoothstep(alpha, 2.0 / 255.0, 0.10)
        * (1.0 - _smoothstep(alpha, 0.82, 0.985))
    )
    semantic_protection = _smoothstep(
        np.clip(semantic_foreground, 0.0, 1.0), 0.62, 0.90
    )
    if protect_subject_screen_color:
        semantic_protection = np.maximum(
            semantic_protection,
            _smoothstep(np.clip(semantic_foreground, 0.0, 1.0), 0.35, 0.72),
        )
    automatic_evidence = (
        np.clip(screen_confidence, 0.0, 1.0)
        * np.square(np.clip(screen_similarity, 0.0, 1.0))
        * (1.0 - semantic_protection)
    )
    # A white or purple glow with green spill is intentionally unlike the
    # screen in aggregate, even though its red/blue channels can still be
    # unmixed safely.  Do not reuse the whole-color similarity gate for those
    # better-conditioned channels or for one-way dominant-screen cleanup.
    automatic_effect_evidence = (
        np.clip(screen_confidence, 0.0, 1.0)
        * (1.0 - semantic_protection)
    )
    automatic_screen_weight = (
        float(np.clip(spill_strength, 0.0, 1.0))
        * soft_coverage
        * _smoothstep(automatic_evidence, 0.62, 0.90)
    )
    automatic_non_screen_weight = (
        float(np.clip(spill_strength * 1.10, 0.0, 1.0))
        * soft_coverage
        * _smoothstep(automatic_effect_evidence, 0.42, 0.78)
    )
    non_screen_direction = (1.0 - screen_direction) * direction_present
    automatic_channel_weight = automatic_screen_weight[:, :, None] + (
        automatic_non_screen_weight - automatic_screen_weight
    )[:, :, None] * non_screen_direction
    manual_weight = np.clip(despill_weight, 0.0, 1.0)
    manual_requested = visible & (manual_weight > (1.0 / 255.0))
    manual_reliable = alpha > (2.0 / 255.0)
    effective_manual_weight = manual_weight * manual_reliable.astype(np.float32)
    requested_channel_weight = np.maximum(
        automatic_channel_weight,
        effective_manual_weight[:, :, None],
    )
    requested = visible & (
        np.any(automatic_channel_weight > (1.0 / 255.0), axis=2)
        | manual_requested
    )

    # Compression and an imperfect alpha often push only one channel outside
    # the physical [0, alpha] box.  Falling back the whole RGB pixel in that
    # case throws away still-valid red/blue glow evidence and retains the green
    # composite.  Recover each channel continuously according to its own box
    # violation instead.
    physical = np.clip(raw, 0.0, alpha3)
    violation = np.maximum(-raw, 0.0) + np.maximum(raw - alpha3, 0.0)
    violation_scale = np.maximum(alpha3 * 0.35, 2.0 / 255.0)
    normalized_violation = violation / violation_scale
    channel_reliability = 1.0 - _smoothstep(
        normalized_violation, 0.08, 0.90
    )
    recovery_weight = requested_channel_weight * channel_reliability
    recovered = immutable_source + (
        physical - immutable_source
    ) * recovery_weight
    severe_channel_fallback = (
        (requested_channel_weight > (1.0 / 255.0))
        & (channel_reliability <= 0.01)
    )
    premultiplied = np.where(
        severe_channel_fallback,
        stable_fallback,
        recovered,
    )
    unreliable_manual = manual_requested & ~manual_reliable
    premultiplied = np.where(
        unreliable_manual[:, :, None],
        stable_fallback,
        premultiplied,
    )
    fallback_required = requested & (
        np.any(severe_channel_fallback, axis=2) | unreliable_manual
    )
    fallback_ratio = float(
        np.count_nonzero(fallback_required)
        / max(np.count_nonzero(requested), 1)
    )

    # Both an authored stroke and high-confidence soft-effect evidence may clean
    # ordinary spill where alpha unmixing is incomplete.  Only lower the
    # screen-aligned excess toward an existing non-screen channel; never move
    # removed screen energy into complementary channels.  This preserves the
    # pure-screen/pure-green no-op contract and prevents magenta fringes.
    dominant = np.argmax(screen_direction, axis=2)
    dominant_violation = np.take_along_axis(
        normalized_violation, dominant[:, :, None], axis=2
    )[:, :, 0]
    dominant_value = np.take_along_axis(
        premultiplied, dominant[:, :, None], axis=2
    )[:, :, 0]
    channel_indices = np.arange(3, dtype=np.int32).reshape(1, 1, 3)
    other_value = np.max(
        np.where(
            channel_indices != dominant[:, :, None],
            premultiplied,
            -1.0,
        ),
        axis=2,
    )
    reliable_physical_other = np.max(
        np.where(
            channel_indices != dominant[:, :, None],
            physical * channel_reliability,
            -1.0,
        ),
        axis=2,
    )
    aligned_excess = np.maximum(dominant_value - other_value, 0.0)
    non_screen_support = np.divide(
        np.maximum(other_value, reliable_physical_other),
        np.maximum(alpha, 1e-5),
        out=np.zeros_like(other_value, dtype=np.float32),
        where=alpha > 1e-5,
    )
    confidence_gate = 0.30 + 0.70 * np.clip(screen_confidence, 0.0, 1.0)
    manual_cleanup_weight = (
        effective_manual_weight
        * _smoothstep(non_screen_support, 0.035, 0.22)
        * confidence_gate
        * (direction_peak[:, :, 0] > 1e-5).astype(np.float32)
    )
    automatic_cleanup_weight = (
        float(np.clip(spill_strength * 1.20, 0.0, 1.0))
        * soft_coverage
        * _smoothstep(automatic_effect_evidence, 0.45, 0.82)
        * _smoothstep(non_screen_support, 0.015, 0.10)
        * _smoothstep(dominant_violation, 0.08, 0.65)
        * confidence_gate
        * (direction_peak[:, :, 0] > 1e-5).astype(np.float32)
    )
    cleanup_weight = np.maximum(
        manual_cleanup_weight,
        automatic_cleanup_weight,
    )
    target_dominant = dominant_value - aligned_excess * cleanup_weight
    for dominant_channel in range(3):
        channel_mask = dominant == dominant_channel
        premultiplied[:, :, dominant_channel] = np.where(
            channel_mask,
            target_dominant,
            premultiplied[:, :, dominant_channel],
        )
    return (
        np.clip(premultiplied, 0, alpha3).astype(np.float32),
        clipping_ratio,
        fallback_ratio,
    )


def _premultiplied_to_straight_rgba(
    premultiplied: np.ndarray, alpha: np.ndarray
) -> np.ndarray:
    safe_alpha = np.maximum(alpha[:, :, None], 1.0 / 255.0)
    straight_linear = np.where(
        alpha[:, :, None] > (1.0 / 255.0), premultiplied / safe_alpha, 0.0
    )
    straight_bgr = _linear_to_srgb(straight_linear)
    alpha_u8 = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
    straight_bgr = _bleed_foreground_rgb(straight_bgr, alpha_u8)
    rgba = cv2.cvtColor(straight_bgr, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = alpha_u8
    return rgba


def prepare_frame_evidence(
    image: np.ndarray,
    options: dict[str, Any],
    *,
    ai_alpha: np.ndarray | None = None,
    screen_model: dict[str, Any] | None = None,
    constraints: np.ndarray | None = None,
    base_alpha: np.ndarray | None = None,
    source_timeline_ordinal: int | None = None,
) -> dict[str, Any]:
    """Prepare all frame-local evidence that is invariant across temporal passes."""

    height, width = image.shape[:2]
    if screen_model is None:
        screen_model = fit_screen_model(
            image,
            options,
            ai_alpha if ai_alpha is not None else base_alpha,
            constraints,
            source_timeline_ordinal,
        )
    plate_bgr, plate_linear, screen_confidence = _materialize_screen_model(
        screen_model, width, height
    )
    observed_linear = _srgb_to_linear(image[:, :, :3])
    distance = _local_screen_distance(image, plate_bgr)
    low = float(options.get("threshold_low", 18.0))
    high = max(low + 1.0, float(options.get("threshold_high", 62.0)))
    distance_alpha = _smoothstep(distance, low, high).astype(np.float32)
    unmix_alpha = _screen_unmix_alpha(
        image, plate_bgr, observed_linear, plate_linear
    ).astype(np.float32) / 255.0
    chroma_alpha = np.where(
        distance_alpha <= (8.0 / 255.0), 0.0, unmix_alpha
    ).astype(np.float32)
    if ai_alpha is None:
        ai = chroma_alpha.copy()
        ai_available = False
    else:
        ai = ai_alpha
        if ai.shape != (height, width):
            ai = cv2.resize(ai, (width, height), interpolation=cv2.INTER_LINEAR)
        ai = np.clip(
            ai.astype(np.float32) / (255.0 if ai.dtype == np.uint8 else 1.0),
            0,
            1,
        )
        ai_available = True
    primary_semantic = (
        _primary_semantic_region(ai)
        if ai_available
        else np.zeros_like(ai, dtype=bool)
    )
    foreground_weight, background_weight, despill_weight = _constraint_weights(
        constraints, (height, width)
    )
    manual_fg = foreground_weight >= 0.95
    manual_bg = background_weight >= 0.95
    connected_screen = _border_connected(distance <= high)
    auto_background = (distance <= low) & (screen_confidence >= 0.58)
    if _key_mode(options) == "preserve_subject_screen_color":
        auto_background &= connected_screen | (ai <= 0.08) | ~primary_semantic
    background_seed = (auto_background | manual_bg) & ~manual_fg
    if _key_mode(options) == "preserve_subject_screen_color":
        auto_foreground = _connected_semantic_core(ai) & ~manual_bg
    else:
        auto_foreground = (
            (ai >= 0.985) & primary_semantic & (distance > low) & ~manual_bg
        )
    foreground_seed = (auto_foreground | manual_fg) & ~manual_bg
    foreground_seed[background_seed] = False
    key_confidence = np.clip(
        0.15
        + screen_confidence
        * (0.45 + 0.4 * np.abs(distance_alpha - 0.5) * 2.0),
        0.05,
        1.0,
    )
    ai_confidence = (
        np.clip(0.22 + np.abs(ai - 0.5) * 1.35, 0.22, 0.95)
        if ai_available
        else np.zeros_like(chroma_alpha)
    )
    return {
        "image": image,
        "options": options,
        "height": height,
        "width": width,
        "screen_model": screen_model,
        "plate_bgr": plate_bgr,
        "plate_linear": plate_linear,
        "screen_confidence": screen_confidence,
        "observed_linear": observed_linear,
        "distance": distance,
        "low": low,
        "high": high,
        "distance_alpha": distance_alpha,
        "chroma_alpha": chroma_alpha,
        "ai": ai,
        "ai_available": ai_available,
        "primary_semantic": primary_semantic,
        "foreground_weight": foreground_weight,
        "background_weight": background_weight,
        "despill_weight": despill_weight,
        "manual_fg": manual_fg,
        "manual_bg": manual_bg,
        "connected_screen": connected_screen,
        "background_seed": background_seed,
        "foreground_seed": foreground_seed,
        "key_confidence": key_confidence,
        "ai_confidence": ai_confidence,
    }


def solve_frame_from_evidence(
    evidence: dict[str, Any],
    *,
    base_alpha: np.ndarray | None = None,
    base_alpha_weight: float = 0.45,
    temporal_alpha: np.ndarray | None = None,
    temporal_premultiplied: np.ndarray | None = None,
    temporal_confidence: np.ndarray | None = None,
    fixed_alpha: np.ndarray | None = None,
    fallback_rgba: np.ndarray | None = None,
    suppress_screen_islands: bool = True,
) -> dict[str, Any]:
    """Solve a frame while reusing its immutable local evidence."""

    image = evidence["image"]
    options = evidence["options"]
    height = evidence["height"]
    width = evidence["width"]
    screen_model = evidence["screen_model"]
    plate_bgr = evidence["plate_bgr"]
    plate_linear = evidence["plate_linear"]
    screen_confidence = evidence["screen_confidence"]
    observed_linear = evidence["observed_linear"]
    distance = evidence["distance"]
    low = evidence["low"]
    high = evidence["high"]
    distance_alpha = evidence["distance_alpha"]
    chroma_alpha = evidence["chroma_alpha"]
    ai = evidence["ai"]
    ai_available = evidence["ai_available"]
    primary_semantic = evidence["primary_semantic"]
    foreground_weight = evidence["foreground_weight"]
    background_weight = evidence["background_weight"]
    despill_weight = evidence["despill_weight"]
    manual_fg = evidence["manual_fg"]
    manual_bg = evidence["manual_bg"]
    connected_screen = evidence["connected_screen"]
    background_seed = evidence["background_seed"]
    foreground_seed = evidence["foreground_seed"]
    key_confidence = evidence["key_confidence"]
    ai_confidence = evidence["ai_confidence"]
    temporal = None
    temporal_weight = np.zeros_like(chroma_alpha)
    if temporal_alpha is not None and temporal_confidence is not None:
        temporal = temporal_alpha
        if temporal.shape != (height, width):
            temporal = cv2.resize(temporal, (width, height), interpolation=cv2.INTER_LINEAR)
        temporal = np.clip(
            temporal.astype(np.float32) / (255.0 if temporal.dtype == np.uint8 else 1.0),
            0,
            1,
        )
        temporal_weight = temporal_confidence
        if temporal_weight.shape != (height, width):
            temporal_weight = cv2.resize(
                temporal_weight, (width, height), interpolation=cv2.INTER_LINEAR
            )
        temporal_weight = np.clip(temporal_weight.astype(np.float32), 0, 1) * 0.82

    numerator = key_confidence * chroma_alpha + ai_confidence * ai
    denominator = key_confidence + ai_confidence
    if base_alpha is not None:
        current = base_alpha
        if current.shape != (height, width):
            current = cv2.resize(
                current, (width, height), interpolation=cv2.INTER_LINEAR
            )
        current = np.clip(
            current.astype(np.float32)
            / (255.0 if current.dtype == np.uint8 else 1.0),
            0,
            1,
        )
        current_weight = np.clip(float(base_alpha_weight), 0.0, 2.0)
        numerator += current_weight * current
        denominator += current_weight
    if temporal is not None:
        numerator += temporal_weight * temporal
        denominator += temporal_weight
    prior = numerator / np.maximum(denominator, 1e-4)
    prior = prior * (1.0 - foreground_weight) + foreground_weight
    prior = prior * (1.0 - background_weight)
    data_weight = np.maximum(0.08, denominator)
    edge_matte = _solve_edge_aware_alpha(
        image[:, :, :3], prior, data_weight, foreground_seed, background_seed
    )

    physical_floor = _physical_alpha_floor(observed_linear, plate_linear)
    feasibility_weight = np.clip(screen_confidence / 0.18, 0, 1) * (
        ~background_seed
    ).astype(np.float32)
    semantic_core = _smoothstep(ai, 0.62, 0.97) * primary_semantic.astype(np.float32)
    if _key_mode(options) == "clean_screen":
        semantic_core *= _smoothstep(distance_alpha, 0.04, 0.28)
    core_matte = np.maximum(semantic_core, foreground_weight)
    core_matte[manual_bg | background_seed] = 0.0

    non_screen_support = _non_screen_color_support(image, plate_bgr)
    effect_matte = (
        chroma_alpha
        * _smoothstep(non_screen_support, 0.06, 0.42)
        * np.clip(screen_confidence / 0.20, 0.0, 1.0)
    )
    if ai_available:
        # Where the semantic model already supplies a soft matte, colour
        # unmixing may recover missed energy but must not turn a translucent
        # complementary-colour glow into an opaque object. AI-missed detached
        # sparks (near-zero AI) remain eligible for the independent effect pass.
        effect_ceiling = np.where(ai > 0.05, np.clip(ai + 0.14, 0.0, 1.0), 1.0)
        effect_matte = np.minimum(effect_matte, effect_ceiling)
    effect_matte[background_seed | manual_bg] = 0.0

    alpha = np.maximum(edge_matte, core_matte)
    alpha = np.maximum(alpha, effect_matte)
    alpha = np.maximum(alpha, physical_floor * feasibility_weight)
    alpha_temporal_blend = np.zeros_like(alpha)
    if temporal is not None:
        temporal_agreement = np.clip(1.0 - np.abs(alpha - temporal) / 0.30, 0, 1)
        temporal_unknown = (
            (alpha > 0.015)
            & (alpha < 0.985)
            & ~foreground_seed
            & ~background_seed
        ).astype(np.float32)
        alpha_temporal_blend = (
            np.minimum(0.48, temporal_weight * 0.58)
            * temporal_agreement
            * temporal_unknown
        )
        alpha = (
            alpha * (1.0 - alpha_temporal_blend)
            + temporal * alpha_temporal_blend
        )
    alpha[foreground_seed] = 1.0
    alpha[background_seed] = 0.0
    alpha = np.clip(alpha, 0, 1).astype(np.float32)
    garbage_stats = {
        "garbage_components_removed": 0,
        "garbage_removed_ratio": 0.0,
        "screen_island_candidates": 0,
    }
    if fixed_alpha is None and suppress_screen_islands:
        alpha, garbage_stats = _suppress_screen_like_islands(
            alpha,
            np.clip((1.0 - distance_alpha) * screen_confidence, 0.0, 1.0),
            non_screen_support,
            primary_semantic,
            manual_fg,
        )
        alpha[manual_fg] = 1.0
        alpha[background_seed | manual_bg] = 0.0
    if fixed_alpha is not None:
        exact_alpha = fixed_alpha
        if exact_alpha.shape != (height, width):
            exact_alpha = cv2.resize(
                exact_alpha, (width, height), interpolation=cv2.INTER_LINEAR
            )
        alpha = np.clip(
            exact_alpha.astype(np.float32)
            / (255.0 if exact_alpha.dtype == np.uint8 else 1.0),
            0,
            1,
        )
    premultiplied, clipping_ratio, fallback_ratio = _recover_premultiplied_linear(
        image,
        alpha,
        plate_bgr,
        float(options.get("spill_strength", 0.72)),
        despill_weight,
        screen_confidence,
        1.0 - distance_alpha,
        ai,
        _key_mode(options) == "preserve_subject_screen_color",
        fallback_rgba,
        observed_linear_bgr=observed_linear,
    )

    temporal_blend = np.zeros_like(alpha)
    if temporal_premultiplied is not None and temporal is not None:
        warped_p = temporal_premultiplied.astype(np.float32)
        if warped_p.shape[:2] != (height, width):
            warped_p = cv2.resize(warped_p, (width, height), interpolation=cv2.INTER_LINEAR)
        agreement = np.clip(1.0 - np.abs(alpha - temporal) / 0.2, 0, 1)
        uncertain = ((alpha > 0.015) & (alpha < 0.985)).astype(np.float32)
        temporal_semantic_protection = _smoothstep(ai, 0.62, 0.90)
        if _key_mode(options) == "preserve_subject_screen_color":
            temporal_semantic_protection = np.maximum(
                temporal_semantic_protection,
                _smoothstep(ai, 0.35, 0.72),
            )
        temporal_color_evidence = (
            np.clip(screen_confidence, 0.0, 1.0)
            * np.square(np.clip(1.0 - distance_alpha, 0.0, 1.0))
            * (1.0 - temporal_semantic_protection)
        )
        temporal_blend = (
            np.minimum(0.32, temporal_weight * agreement)
            * uncertain
            * _smoothstep(temporal_color_evidence, 0.45, 0.82)
        )
        temporal_blend[foreground_seed | background_seed] = 0.0
        premultiplied = (
            premultiplied * (1.0 - temporal_blend[:, :, None])
            + warped_p * temporal_blend[:, :, None]
        )
        premultiplied = np.clip(premultiplied, 0, alpha[:, :, None])

    rgba = _premultiplied_to_straight_rgba(premultiplied, alpha)
    alpha_u8 = rgba[:, :, 3]
    visible = alpha_u8 > 16
    area_ratio = float(np.count_nonzero(visible) / max(alpha_u8.size, 1))
    touches_edge = bool(
        np.any(visible[0, :])
        or np.any(visible[-1, :])
        or np.any(visible[:, 0])
        or np.any(visible[:, -1])
    )
    _, component_stats = _clean_components(alpha_u8)
    residue_ratio = _screen_residue_ratio(
        rgba[:, :, :3], alpha_u8, plate_bgr, background_seed
    )
    residue, _, _ = _screen_aligned_excess(rgba[:, :, :3], plate_bgr)
    residue_map = np.clip(residue * (alpha_u8.astype(np.float32) / 255.0), 0, 255).astype(np.uint8)
    trimap = np.full((height, width), 128, dtype=np.uint8)
    trimap[background_seed] = 0
    trimap[foreground_seed] = 255
    combined_confidence = np.clip(
        denominator / max(float(np.percentile(denominator, 95)), 1e-4), 0, 1
    )
    route_stack = np.stack([core_matte, edge_matte, effect_matte], axis=0)
    route_strength = np.max(route_stack, axis=0)
    route_winner = np.argmax(route_stack, axis=0)
    route_active = route_strength > 0.02
    uncertain = (alpha_u8 > 16) & (alpha_u8 < 239)
    screen_distances = distance[connected_screen]
    if screen_distances.size < 64:
        screen_distances = np.concatenate(
            [distance[0, :], distance[-1, :], distance[:, 0], distance[:, -1]]
        )
    recommended_low = float(
        np.clip(np.percentile(screen_distances, 90) + 2.0, 4.0, 48.0)
    )
    recommended_high = float(
        np.clip(
            max(recommended_low + 12.0, np.percentile(screen_distances, 99.5) + 8.0),
            16.0,
            100.0,
        )
    )
    enclosed_screen = (distance <= high) & ~connected_screen & (ai >= 0.35)
    color_conflict_ratio = float(
        np.count_nonzero(enclosed_screen) / max(alpha_u8.size, 1)
    )
    background_bgr = np.median(plate_bgr.reshape(-1, 3), axis=0).astype(np.uint8)
    qc = {
        **component_stats,
        **garbage_stats,
        "area_ratio": area_ratio,
        "uncertain_ratio": float(np.count_nonzero(uncertain) / max(alpha_u8.size, 1)),
        "empty_mask": area_ratio < 0.0005,
        "touches_edge": touches_edge,
        "multiple_subjects": component_stats.get("secondary_ratio", 0) > 0.35,
        "fragmented_mask": component_stats.get("components", 0) > 8,
        "screen_residue_ratio": residue_ratio,
        "screen_residue": residue_ratio > 0.05,
        "opaque_screen_ratio": float(
            np.count_nonzero(background_seed & (alpha_u8 >= 239))
            / max(np.count_nonzero(visible), 1)
        ),
        "screen_model_confidence": float(np.mean(screen_confidence)),
        "screen_model_low_confidence": float(np.mean(screen_confidence)) < 0.28,
        "matte_confidence": float(np.mean(combined_confidence)),
        "core_matte_ratio": float(np.mean(core_matte > 0.5)),
        "edge_matte_ratio": float(np.mean((edge_matte > 0.02) & (edge_matte < 0.98))),
        "effect_matte_ratio": float(np.mean(effect_matte > 0.02)),
        "core_route_winner_ratio": float(np.mean(route_active & (route_winner == 0))),
        "edge_route_winner_ratio": float(np.mean(route_active & (route_winner == 1))),
        "effect_route_winner_ratio": float(np.mean(route_active & (route_winner == 2))),
        "sequence_plate_frames": int(screen_model.get("sequence_plate_frames") or 1),
        "sequence_plate_support": float(screen_model.get("sequence_plate_support") or 0.0),
        "sequence_plate_confidence": float(screen_model.get("sequence_plate_confidence") or 0.0),
        "reconstruction_clipping_ratio": clipping_ratio,
        "reconstruction_clipping": clipping_ratio > 0.02,
        "color_fallback_ratio": fallback_ratio,
        "ai_assist": ai_available,
        "temporal_assist": temporal is not None,
        "temporal_alpha_acceptance_ratio": float(
            np.mean(alpha_temporal_blend > 0.01)
        ),
        "temporal_acceptance_ratio": float(np.mean(temporal_blend > 0.01)),
        "key_mode": _key_mode(options),
        "screen_lock_ratio": float(
            np.count_nonzero(background_seed) / max(alpha_u8.size, 1)
        ),
        "color_conflict_ratio": color_conflict_ratio,
        "color_conflict": color_conflict_ratio > 0.01,
        "background_bgr": [int(channel) for channel in background_bgr],
        "background_rgb": [
            int(background_bgr[2]),
            int(background_bgr[1]),
            int(background_bgr[0]),
        ],
        "background_palette_rgb": [
            [int(sample[2]), int(sample[1]), int(sample[0])]
            for sample in _screen_palette_for_options(image, options)
        ],
        "removed_ratio": float(np.count_nonzero(alpha_u8 <= 16) / max(alpha_u8.size, 1)),
        "connected_screen_ratio": float(
            np.count_nonzero(connected_screen) / max(alpha_u8.size, 1)
        ),
        "border_clear_ratio": float(
            np.count_nonzero(
                np.concatenate(
                    [alpha_u8[0, :], alpha_u8[-1, :], alpha_u8[:, 0], alpha_u8[:, -1]]
                )
                <= 16
            )
            / max(alpha_u8.shape[1] * 2 + alpha_u8.shape[0] * 2, 1)
        ),
        "recommended_low": round(recommended_low, 1),
        "recommended_high": round(recommended_high, 1),
        "algorithm_version": HYBRID_MATTE_VERSION,
        "color_recovery_revision": CHROMA_COLOR_RECOVERY_REVISION,
    }
    return {
        "rgba": rgba,
        "alpha": alpha_u8,
        "alpha_float": alpha,
        "premultiplied": premultiplied,
        "trimap": trimap,
        "confidence": combined_confidence,
        "residue": residue_map,
        "screen_model": screen_model,
        "plate_bgr": plate_bgr,
        "screen_confidence": screen_confidence,
        "qc": qc,
    }


def chroma_rgba(image: np.ndarray, options: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    palette_bgr = _screen_palette_for_options(image, options)
    background_bgr = np.median(palette_bgr, axis=0).astype(np.uint8)
    screen_plate = _spatial_screen_plate_bgr(image, palette_bgr)
    observed_linear = _srgb_to_linear(image[:, :, :3])
    plate_linear = _srgb_to_linear(screen_plate)

    distance = _screen_distance(image, palette_bgr)
    low = float(options.get("threshold_low", 18.0))
    high = max(low + 1.0, float(options.get("threshold_high", 62.0)))
    distance_alpha = (_smoothstep(distance, low, high) * 255.0).astype(np.uint8)
    # Soft alpha benefits from a lightly smoothed distance field, but the
    # authoritative screen lock must use raw pixels. Otherwise a 1-3 px green
    # gap beside dark artwork is blurred above the low threshold and filled back.
    screen_lock = _screen_distance(image, palette_bgr, smooth=False) <= low
    unmix_alpha = _screen_unmix_alpha(
        image, screen_plate, observed_linear, plate_linear
    )
    # Distance supplies a robust opaque core; unmixing retains motion blur and
    # translucent effects. Taking the maximum prevents soft artwork from becoming
    # more transparent merely because it contains a small amount of screen color.
    soft_alpha = np.where(
        distance_alpha <= 8,
        0,
        np.maximum(unmix_alpha, distance_alpha),
    ).astype(np.uint8)
    # Track which screen-like pixels connect to the outer frame for diagnostics.
    # Alpha itself remains color-driven so enclosed backdrop gaps (for example
    # between crossed arms) are not accidentally filled back into the subject.
    connected_screen = _border_connected(distance <= high)
    alpha = soft_alpha

    cleanup_radius = int(options.get("cleanup_radius", 2))
    if cleanup_radius > 0:
        kernel_size = cleanup_radius * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        alpha = cv2.morphologyEx(alpha, cv2.MORPH_OPEN, kernel)
        alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, kernel)
        alpha[screen_lock] = 0

    alpha, component_stats = _clean_components(alpha)
    alpha = _fill_small_holes(alpha, screen_lock)
    alpha[screen_lock] = 0
    feather = int(options.get("feather", 3))
    if feather > 0:
        kernel_size = feather * 2 + 1
        alpha = cv2.GaussianBlur(alpha, (kernel_size, kernel_size), 0)
    # The screen lock is authoritative and is re-applied after every operation
    # that can grow alpha into an enclosed green gap.
    alpha[screen_lock] = 0

    spill_strength = float(options.get("spill_strength", 0.72))
    rgba = _compose_straight_alpha_rgba(
        image,
        alpha,
        spill_strength,
        screen_plate,
        observed_linear,
        plate_linear,
        not bool(options.get("_skip_despill", False)),
        int(options.get("_bleed_iterations", 8)),
    )
    corrected = rgba[:, :, :3]

    foreground = alpha > 16
    area_ratio = float(np.count_nonzero(foreground) / foreground.size)
    touches_edge = bool(
        np.any(foreground[0, :])
        or np.any(foreground[-1, :])
        or np.any(foreground[:, 0])
        or np.any(foreground[:, -1])
    )
    uncertain_ratio = float(np.count_nonzero((alpha > 16) & (alpha < 239)) / alpha.size)
    screen_residue_ratio = _screen_residue_ratio(
        corrected, alpha, screen_plate, screen_lock
    )
    opaque_screen_ratio = float(
        np.count_nonzero(screen_lock & (alpha >= 239)) / max(np.count_nonzero(alpha > 16), 1)
    )
    border_distance = np.concatenate(
        [distance[0, :], distance[-1, :], distance[:, 0], distance[:, -1]]
    )
    source_f32 = image[:, :, :3].astype(np.float32)
    source_range = source_f32.max(axis=2) - source_f32.min(axis=2)
    screen_like = (distance <= high) & (source_range > 18.0)
    enclosed_screen = screen_like & ~connected_screen
    color_conflict_ratio = float(np.count_nonzero(enclosed_screen) / max(alpha.size, 1))
    suggestion_distance = distance[screen_like] if np.count_nonzero(screen_like) >= 64 else border_distance
    recommended_low = float(np.clip(np.percentile(suggestion_distance, 90) + 2.0, 4.0, 48.0))
    recommended_high = float(
        np.clip(max(recommended_low + 12.0, np.percentile(suggestion_distance, 99.5) + 8.0), 16.0, 100.0)
    )
    qc = {
        **component_stats,
        "area_ratio": area_ratio,
        "uncertain_ratio": uncertain_ratio,
        "empty_mask": area_ratio < 0.0005,
        "touches_edge": touches_edge,
        "multiple_subjects": component_stats.get("secondary_ratio", 0) > 0.35,
        "fragmented_mask": component_stats.get("components", 0) > 8,
        "screen_residue_ratio": screen_residue_ratio,
        "screen_residue": screen_residue_ratio > 0.05 or opaque_screen_ratio > 0.00002,
        "opaque_screen_ratio": opaque_screen_ratio,
        "key_mode": _key_mode(options),
        "screen_lock_ratio": float(np.count_nonzero(screen_lock) / max(alpha.size, 1)),
        "color_conflict_ratio": color_conflict_ratio,
        "color_conflict": color_conflict_ratio > 0.01,
        "background_bgr": [int(channel) for channel in background_bgr],
        "background_rgb": [int(background_bgr[2]), int(background_bgr[1]), int(background_bgr[0])],
        "background_palette_rgb": [
            [int(sample[2]), int(sample[1]), int(sample[0])] for sample in palette_bgr
        ],
        "removed_ratio": float(np.count_nonzero(alpha <= 16) / alpha.size),
        "connected_screen_ratio": float(np.count_nonzero(connected_screen) / alpha.size),
        "border_clear_ratio": float(
            np.count_nonzero(
                np.concatenate([alpha[0, :], alpha[-1, :], alpha[:, 0], alpha[:, -1]]) <= 16
            )
            / max(alpha.shape[1] * 2 + alpha.shape[0] * 2, 1)
        ),
        "recommended_low": round(recommended_low, 1),
        "recommended_high": round(recommended_high, 1),
    }
    return rgba, alpha, qc
