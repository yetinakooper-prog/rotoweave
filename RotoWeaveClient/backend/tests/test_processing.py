from __future__ import annotations

import json
import cv2
import numpy as np
import pytest
from pathlib import Path

from backend.app.processing import (
    _conservative_manual_despill_bgr,
    _recover_premultiplied_linear,
    chroma_rgba,
    fit_screen_model,
    prepare_frame_evidence,
    screen_plate_for_qc,
    solve_frame_from_evidence,
    stabilize_screen_models,
)
from backend.app.schemas import ChromaSettings
from backend.app.versions import (
    HYBRID_MATTE_VERSION,
    CHROMA_COLOR_RECOVERY_REVISION,
)


def _prepare_and_solve(image: np.ndarray, options: dict, **kwargs: object) -> dict:
    evidence_keys = {
        "ai_alpha",
        "screen_model",
        "constraints",
        "base_alpha",
        "source_timeline_ordinal",
    }
    evidence = prepare_frame_evidence(
        image,
        options,
        **{key: value for key, value in kwargs.items() if key in evidence_keys},
    )
    return solve_frame_from_evidence(
        evidence,
        **{key: value for key, value in kwargs.items() if key not in evidence_keys},
    )


def _test_to_linear(values: np.ndarray) -> np.ndarray:
    normalized = values.astype(np.float32) / 255.0
    return np.where(
        normalized <= 0.04045,
        normalized / 12.92,
        np.power((normalized + 0.055) / 1.055, 2.4),
    )


def _test_to_srgb(values: np.ndarray) -> np.ndarray:
    encoded = np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    )
    return np.clip(encoded * 255.0, 0, 255).astype(np.uint8)


def _synthetic_translucent_screen() -> tuple[
    np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, object]
]:
    height, width = 128, 160
    yy, xx = np.mgrid[:height, :width]
    plate = np.zeros((height, width, 3), dtype=np.uint8)
    plate[:, :, 0] = 8 + (xx * 10 / width).astype(np.uint8)
    plate[:, :, 1] = 170 + (
        xx * 35 / width + yy * 20 / height
    ).astype(np.uint8)
    plate[:, :, 2] = 5 + (yy * 8 / height).astype(np.uint8)
    alpha = np.zeros((height, width), dtype=np.float32)
    body = ((xx - 78) / 30) ** 2 + ((yy - 73) / 48) ** 2 <= 1
    alpha[body] = 1.0
    for offset in (-38, -34, 34, 38):
        hair = np.abs(
            xx - (78 + offset + (yy - 35) * 0.15 * np.sign(offset))
        ) < 1.2
        hair &= (yy > 15) & (yy < 62)
        alpha[hair] = np.maximum(alpha[hair], 0.55)
    glow = np.exp(
        -(((xx - 126) / 12) ** 2 + ((yy - 38) / 9) ** 2) * 1.5
    ) * 0.52
    alpha = np.maximum(alpha, glow.astype(np.float32))
    foreground = np.zeros((height, width, 3), dtype=np.uint8)
    foreground[:] = (35, 45, 230)
    foreground[glow > 0.01] = (30, 225, 255)
    observed = _test_to_srgb(
        _test_to_linear(foreground) * alpha[:, :, None]
        + _test_to_linear(plate) * (1.0 - alpha[:, :, None])
    )
    options: dict[str, object] = {
        "screen_samples": [
            {
                "rgb": [5, 180, 10],
                "x": 0.0,
                "y": 0.0,
                "source_timeline_ordinal": 0,
            }
        ],
        "threshold_low": 12,
        "threshold_high": 62,
        "cleanup_radius": 1,
        "feather": 2,
        "spill_strength": 0.72,
        "key_mode": "clean_screen",
    }
    return observed, plate, foreground, alpha, options


def test_chroma_key_keeps_subject_and_removes_green() -> None:
    image = np.full((96, 128, 3), (0, 220, 0), dtype=np.uint8)
    cv2.rectangle(image, (42, 18), (86, 87), (25, 35, 225), thickness=-1)

    rgba, alpha, qc = chroma_rgba(
        image,
        {
            "threshold_low": 12,
            "threshold_high": 48,
            "cleanup_radius": 1,
            "feather": 1,
            "spill_strength": 0.7,
        },
    )

    assert rgba.shape == (96, 128, 4)
    assert int(alpha[48, 64]) > 245
    assert int(alpha[5, 5]) < 5
    assert not qc["empty_mask"]
    assert not qc["touches_edge"]
    assert 0.15 < qc["area_ratio"] < 0.35


def test_chroma_thresholds_materially_change_uneven_screen() -> None:
    image = np.full((96, 128, 3), (0, 220, 0), dtype=np.uint8)
    image[:, 64:] = (15, 150, 10)
    cv2.rectangle(image, (42, 18), (86, 87), (25, 35, 225), thickness=-1)
    common = {
        "screen_samples": [{"rgb": [0, 220, 0]}],
        "cleanup_radius": 0,
        "feather": 0,
        "spill_strength": 0.7,
    }

    _, strict_alpha, _ = chroma_rgba(
        image, {**common, "threshold_low": 4, "threshold_high": 12}
    )
    _, tolerant_alpha, _ = chroma_rgba(
        image, {**common, "threshold_low": 45, "threshold_high": 100}
    )

    assert int(strict_alpha[5, 100]) > 240
    assert int(tolerant_alpha[5, 100]) < 5
    assert int(tolerant_alpha[48, 64]) > 245


def test_auto_palette_clears_bright_and_shadowed_green() -> None:
    image = np.full((96, 128, 3), (0, 220, 0), dtype=np.uint8)
    image[:, 64:] = (15, 150, 10)
    cv2.rectangle(image, (42, 18), (86, 87), (25, 35, 225), thickness=-1)

    _, alpha, qc = chroma_rgba(
        image,
        {
            "threshold_low": 12,
            "threshold_high": 48,
            "cleanup_radius": 0,
            "feather": 0,
            "spill_strength": 0.7,
        },
    )

    assert int(alpha[5, 5]) < 5
    assert int(alpha[5, 100]) < 5
    assert len(qc["background_palette_rgb"]) >= 2


def test_large_transparent_gap_is_not_filled_as_subject() -> None:
    image = np.full((120, 120, 3), (0, 220, 0), dtype=np.uint8)
    cv2.rectangle(image, (20, 15), (100, 105), (25, 35, 225), thickness=12)

    _, alpha, _ = chroma_rgba(
        image,
        {
            "threshold_low": 12,
            "threshold_high": 48,
            "cleanup_radius": 0,
            "feather": 0,
            "spill_strength": 0.7,
        },
    )

    assert int(alpha[60, 60]) < 5
    assert int(alpha[18, 60]) > 245


def test_clean_screen_locks_enclosed_green_without_removing_detached_effects() -> None:
    image = np.full((120, 160, 3), (0, 220, 0), dtype=np.uint8)
    cv2.rectangle(image, (40, 15), (110, 105), (30, 30, 220), thickness=-1)
    cv2.rectangle(image, (55, 35), (95, 85), (0, 220, 0), thickness=-1)
    cv2.circle(image, (132, 30), 4, (220, 80, 20), thickness=-1)

    _, alpha, qc = chroma_rgba(
        image,
        {
            "threshold_low": 12,
            "threshold_high": 48,
            "cleanup_radius": 2,
            "feather": 1,
            "spill_strength": 0.72,
            "key_mode": "clean_screen",
        },
    )

    assert int(alpha[30, 132]) > 245  # Detached spark survives component cleanup.
    assert int(alpha[60, 75]) < 5  # Enclosed screen-colored gap cannot be filled back.
    assert qc["components"] >= 2
    assert qc["color_conflict"]  # Preserve mode can use AI/manual review for ambiguous green.


def test_screen_lock_survives_close_hole_fill_and_feather() -> None:
    image = np.full((80, 80, 3), (0, 220, 0), dtype=np.uint8)
    cv2.rectangle(image, (16, 10), (64, 72), (30, 30, 220), thickness=-1)
    image[39:42, 39:42] = (0, 220, 0)

    _, alpha, qc = chroma_rgba(
        image,
        {
            "screen_samples": [{"rgb": [0, 220, 0]}],
            "threshold_low": 12,
            "threshold_high": 48,
            "cleanup_radius": 3,
            "feather": 3,
            "spill_strength": 0.72,
            "key_mode": "clean_screen",
        },
    )

    assert np.all(alpha[39:42, 39:42] == 0)
    assert int(alpha[40, 32]) > 245
    assert qc["opaque_screen_ratio"] == 0




def test_hybrid_v4_handles_translucent_subject_on_uneven_screen() -> None:
    observed, _, foreground, expected_alpha, options = (
        _synthetic_translucent_screen()
    )
    old_rgba, old_alpha, _ = chroma_rgba(observed, options)
    solved = _prepare_and_solve(
        observed,
        options,
        ai_alpha=np.clip(expected_alpha * 255.0, 0, 255).astype(np.uint8),
        source_timeline_ordinal=0,
    )

    def alpha_mae(alpha: np.ndarray) -> float:
        return float(
            np.mean(np.abs(alpha.astype(np.float32) / 255.0 - expected_alpha))
        )

    def composite_mae(rgba: np.ndarray, background: np.ndarray) -> float:
        alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
        actual = (
            _test_to_linear(rgba[:, :, :3]) * alpha
            + _test_to_linear(background) * (1.0 - alpha)
        )
        expected = (
            _test_to_linear(foreground) * expected_alpha[:, :, None]
            + _test_to_linear(background) * (1.0 - expected_alpha[:, :, None])
        )
        return float(np.mean(np.abs(actual - expected)))

    backgrounds = [
        np.zeros_like(observed),
        np.full_like(observed, 255),
        np.full_like(observed, (255, 0, 255)),
    ]
    old_composite_mae = float(
        np.mean([composite_mae(old_rgba, item) for item in backgrounds])
    )
    new_composite_mae = float(
        np.mean([composite_mae(solved["rgba"], item) for item in backgrounds])
    )

    assert alpha_mae(solved["alpha"]) <= alpha_mae(old_alpha) * 0.80
    assert new_composite_mae <= old_composite_mae * 0.75
    assert 48 < int(solved["alpha"][38, 126]) < 220
    assert solved["qc"]["algorithm_version"] == HYBRID_MATTE_VERSION
    assert (
        solved["qc"]["color_recovery_revision"]
        == CHROMA_COLOR_RECOVERY_REVISION
    )
    assert solved["qc"]["reconstruction_clipping_ratio"] < 0.02
    assert np.array_equal(solved["rgba"][:, :, 3], solved["alpha"])


def test_screen_plate_for_qc_restores_compact_frame_model() -> None:
    projection = {
        "revision": CHROMA_COLOR_RECOVERY_REVISION,
        "rows": 1,
        "cols": 1,
        "rgb": [[255, 0, 0]],
        "confidence": [255],
    }

    plate = screen_plate_for_qc((2, 3), {}, screen_model=projection)

    assert plate.shape == (2, 3, 3)
    expected_bgr = np.array([0, 0, 255], dtype=np.int16)
    assert np.max(np.abs(plate.astype(np.int16) - expected_bgr)) <= 1


@pytest.mark.parametrize(
    "effect_bgr",
    [
        (240, 240, 240),  # white light
        (25, 225, 245),  # yellow light
        (25, 25, 240),  # red light
        (235, 225, 25),  # cyan light
        (150, 150, 150),  # neutral smoke
    ],
)
def test_v4_recovers_translucent_effect_colors_after_compression(
    effect_bgr: tuple[int, int, int],
) -> None:
    height, width = 96, 128
    yy, xx = np.mgrid[:height, :width]
    plate = np.full((height, width, 3), (8, 210, 5), dtype=np.uint8)
    alpha = (
        np.exp(-(((xx - 64) / 18) ** 2 + ((yy - 48) / 11) ** 2)) * 0.58
    ).astype(np.float32)
    motion_blur = (np.abs(yy - (30 + xx * 0.18)) < 1.4) & (xx > 15) & (xx < 70)
    alpha[motion_blur] = np.maximum(alpha[motion_blur], 0.34)
    spark = (xx - 103) ** 2 + (yy - 21) ** 2 <= 7
    alpha[spark] = 0.88
    foreground = np.full((height, width, 3), effect_bgr, dtype=np.uint8)
    foreground[spark] = (245, 245, 255)
    observed = _test_to_srgb(
        _test_to_linear(foreground) * alpha[:, :, None]
        + _test_to_linear(plate) * (1.0 - alpha[:, :, None])
    )
    encoded_ok, encoded = cv2.imencode(
        ".jpg", observed, [cv2.IMWRITE_JPEG_QUALITY, 82]
    )
    assert encoded_ok
    compressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    options = {
        "screen_samples": [{"rgb": [5, 210, 8]}],
        "threshold_low": 12,
        "threshold_high": 58,
        "spill_strength": 0.72,
        "key_mode": "clean_screen",
    }
    ai_alpha = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
    ai_alpha[spark] = 0  # Detached effects cannot depend on a person-only core.
    old_rgba, old_alpha, _ = chroma_rgba(compressed, options)
    solved = _prepare_and_solve(compressed, options, ai_alpha=ai_alpha)
    expected = alpha
    old_error = float(
        np.mean(np.abs(old_alpha.astype(np.float32) / 255.0 - expected))
    )
    new_error = float(
        np.mean(np.abs(solved["alpha"].astype(np.float32) / 255.0 - expected))
    )

    assert new_error <= old_error * 0.80, (effect_bgr, old_error, new_error)
    assert int(solved["alpha"][21, 103]) > 100
    assert int(solved["alpha"][48, 64]) < 220
    assert solved["qc"]["components"] >= 2


def test_v4_does_not_regress_plain_opaque_subject_over_five_percent() -> None:
    image = np.full((96, 128, 3), (0, 220, 0), dtype=np.uint8)
    expected = np.zeros((96, 128), dtype=np.uint8)
    cv2.rectangle(image, (38, 10), (90, 88), (30, 35, 225), thickness=-1)
    cv2.rectangle(expected, (38, 10), (90, 88), 255, thickness=-1)
    options = {
        "screen_samples": [{"rgb": [0, 220, 0]}],
        "threshold_low": 12,
        "threshold_high": 48,
        "cleanup_radius": 1,
        "feather": 1,
        "spill_strength": 0.72,
    }
    _, old_alpha, _ = chroma_rgba(image, options)
    solved = _prepare_and_solve(image, options, ai_alpha=expected)
    old_error = float(
        np.mean(np.abs(old_alpha.astype(np.float32) - expected.astype(np.float32)))
    )
    new_error = float(
        np.mean(
            np.abs(
                solved["alpha"].astype(np.float32) - expected.astype(np.float32)
            )
        )
    )
    assert new_error <= old_error * 1.05 + 0.5
    # Transparent texels next to the subject carry clean foreground color so
    # Unity bilinear filtering cannot pull the original green screen inward.
    assert int(solved["alpha"][48, 34]) == 0
    assert int(solved["rgba"][48, 34, 2]) > 120
    assert int(solved["rgba"][48, 34, 1]) < 100
    opaque_core = expected == 255
    core_rgb_error = np.abs(
        solved["rgba"][:, :, :3][opaque_core].astype(np.int16)
        - image[:, :, :3][opaque_core].astype(np.int16)
    )
    assert int(np.max(core_rgb_error)) <= 1


def test_v4_conservative_auto_color_preserves_ambiguous_semantic_subject() -> None:
    height, width = 24, 32
    image = np.full((height, width, 3), (150, 150, 160), dtype=np.uint8)
    fixed_alpha = np.full((height, width), 191, dtype=np.uint8)
    ai_alpha = np.full((height, width), 224, dtype=np.uint8)
    screen_model = {
        "grid_bgr": np.full((8, 8, 3), (8, 220, 5), dtype=np.float32),
        "grid_linear": _test_to_linear(
            np.full((8, 8, 3), (8, 220, 5), dtype=np.uint8)
        ),
        "grid_confidence": np.ones((8, 8), dtype=np.float32),
    }

    solved = _prepare_and_solve(
        image,
        {
            "screen_samples": [{"rgb": [5, 220, 8]}],
            "threshold_low": 12,
            "threshold_high": 48,
            "spill_strength": 1.0,
        },
        ai_alpha=ai_alpha,
        screen_model=screen_model,
        fixed_alpha=fixed_alpha,
    )

    assert np.array_equal(solved["alpha"], fixed_alpha)
    rgb_error = np.abs(
        solved["rgba"][:, :, :3].astype(np.int16) - image.astype(np.int16)
    )
    assert int(np.max(rgb_error)) <= 1


def test_v4_channelwise_recovery_keeps_valid_non_screen_channels() -> None:
    height, width = 12, 16
    alpha = np.full((height, width), 0.20, dtype=np.float32)
    plate_bgr = np.full((height, width, 3), (5, 210, 4), dtype=np.uint8)
    plate_linear = _test_to_linear(plate_bgr)
    foreground_linear = np.full((height, width, 3), (0.80, 0.60, 0.80), dtype=np.float32)
    observed_linear = (
        foreground_linear * alpha[:, :, None]
        + plate_linear * (1.0 - alpha[:, :, None])
    )
    # Simulate a compressed green highlight whose green channel alone is no
    # longer feasible for the supplied alpha.  Blue/red remain valid evidence.
    observed_linear[:, :, 1] = np.clip(observed_linear[:, :, 1] + 0.12, 0, 1)
    observed = _test_to_srgb(observed_linear)

    premultiplied, _, fallback_ratio = _recover_premultiplied_linear(
        observed,
        alpha,
        plate_bgr,
        1.0,
        np.zeros_like(alpha),
        np.ones_like(alpha),
        np.ones_like(alpha),
        np.zeros_like(alpha),
        False,
    )
    straight = premultiplied / alpha[:, :, None]
    immutable_straight = _test_to_linear(observed)

    assert fallback_ratio < 1.0
    assert float(np.mean(straight[:, :, 0])) > 0.70
    assert float(np.mean(straight[:, :, 2])) > 0.70
    assert float(np.mean(straight[:, :, [0, 2]] - immutable_straight[:, :, [0, 2]])) > 0.45


def test_v4_auto_recovers_low_alpha_white_glow_without_green_fringe() -> None:
    height, width = 16, 20
    alpha = np.full((height, width), 0.08, dtype=np.float32)
    plate_bgr = np.full((height, width, 3), (5, 210, 4), dtype=np.uint8)
    plate_linear = _test_to_linear(plate_bgr)
    foreground_linear = np.full((height, width, 3), 0.80, dtype=np.float32)
    observed_linear = (
        foreground_linear * alpha[:, :, None]
        + plate_linear * (1.0 - alpha[:, :, None])
    )
    observed = _test_to_srgb(observed_linear)

    premultiplied, _, _ = _recover_premultiplied_linear(
        observed,
        alpha,
        plate_bgr,
        1.0,
        np.zeros_like(alpha),
        np.full_like(alpha, 0.72),
        np.full_like(alpha, 0.95),
        np.zeros_like(alpha),
        False,
    )
    straight = premultiplied / alpha[:, :, None]
    immutable_straight = _test_to_linear(observed)
    baseline_excess = immutable_straight[:, :, 1] - np.maximum(
        immutable_straight[:, :, 0], immutable_straight[:, :, 2]
    )
    recovered_excess = straight[:, :, 1] - np.maximum(
        straight[:, :, 0], straight[:, :, 2]
    )

    assert float(np.mean(recovered_excess)) <= float(np.mean(baseline_excess)) * 0.15
    assert float(np.mean(straight[:, :, [0, 2]])) > 0.45


def test_v4_auto_despill_keeps_pure_screen_color_when_no_foreground_support() -> None:
    height, width = 10, 14
    alpha = np.full((height, width), 0.08, dtype=np.float32)
    plate_bgr = np.full((height, width, 3), (5, 210, 4), dtype=np.uint8)
    expected = _test_to_linear(plate_bgr) * alpha[:, :, None]

    premultiplied, _, _ = _recover_premultiplied_linear(
        plate_bgr,
        alpha,
        plate_bgr,
        1.0,
        np.zeros_like(alpha),
        np.ones_like(alpha),
        np.ones_like(alpha),
        np.zeros_like(alpha),
        False,
    )

    assert np.allclose(premultiplied, expected, atol=1e-6)


def test_v4_auto_despill_preserves_valid_translucent_green_effect() -> None:
    height, width = 10, 14
    alpha = np.full((height, width), 0.20, dtype=np.float32)
    plate_bgr = np.full((height, width, 3), (5, 210, 4), dtype=np.uint8)
    foreground_bgr = np.full((height, width, 3), (100, 200, 100), dtype=np.uint8)
    observed = _test_to_srgb(
        _test_to_linear(foreground_bgr) * alpha[:, :, None]
        + _test_to_linear(plate_bgr) * (1.0 - alpha[:, :, None])
    )

    premultiplied, _, _ = _recover_premultiplied_linear(
        observed,
        alpha,
        plate_bgr,
        1.0,
        np.zeros_like(alpha),
        np.ones_like(alpha),
        np.ones_like(alpha),
        np.zeros_like(alpha),
        False,
    )
    straight = premultiplied / alpha[:, :, None]

    assert float(np.mean(straight[:, :, 1])) > float(
        np.mean(np.maximum(straight[:, :, 0], straight[:, :, 2]))
    ) * 1.5


def test_color_recovery_prefers_supplied_original_linear_authority() -> None:
    height, width = 5, 7
    proxy_bgr = np.zeros((height, width, 3), dtype=np.uint8)
    plate_bgr = np.zeros_like(proxy_bgr)
    alpha = np.full((height, width), 0.5, dtype=np.float32)
    original_linear_bgr = np.zeros((height, width, 3), dtype=np.float32)
    original_linear_bgr[:, :, 0] = 0.80
    original_linear_bgr[:, :, 1] = 0.30
    original_linear_bgr[:, :, 2] = 0.10

    premultiplied, _, _ = _recover_premultiplied_linear(
        proxy_bgr,
        alpha,
        plate_bgr,
        0.0,
        np.zeros_like(alpha),
        np.zeros_like(alpha),
        np.zeros_like(alpha),
        np.zeros_like(alpha),
        False,
        observed_linear_bgr=original_linear_bgr,
    )

    expected = np.minimum(original_linear_bgr * alpha[:, :, None], alpha[:, :, None])
    assert np.allclose(premultiplied, expected, atol=1e-6)
    assert float(premultiplied[0, 0, 0]) > float(premultiplied[0, 0, 2])


def test_v4_auto_despill_keeps_desaturated_screen_without_foreground_support() -> None:
    height, width = 10, 14
    alpha = np.full((height, width), 0.08, dtype=np.float32)
    plate_bgr = np.full((height, width, 3), (80, 210, 80), dtype=np.uint8)
    expected = _test_to_linear(plate_bgr) * alpha[:, :, None]

    premultiplied, _, _ = _recover_premultiplied_linear(
        plate_bgr,
        alpha,
        plate_bgr,
        1.0,
        np.zeros_like(alpha),
        np.ones_like(alpha),
        np.ones_like(alpha),
        np.zeros_like(alpha),
        False,
    )

    assert np.allclose(premultiplied, expected, atol=1e-6)


def test_manual_despill_does_not_turn_retained_green_into_magenta() -> None:
    image = np.full((32, 32, 3), (5, 220, 5), dtype=np.uint8)
    fixed_alpha = np.full((32, 32), 128, dtype=np.uint8)
    constraints = np.zeros((32, 32, 3), dtype=np.uint8)
    constraints[:, :, 2] = 255

    solved = _prepare_and_solve(
        image,
        {
            "screen_samples": [{"rgb": [5, 220, 5]}],
            "threshold_low": 12,
            "threshold_high": 48,
            "spill_strength": 1.0,
        },
        ai_alpha=np.zeros((32, 32), dtype=np.uint8),
        constraints=constraints,
        fixed_alpha=fixed_alpha,
    )

    assert np.array_equal(solved["alpha"], fixed_alpha)
    result_bgr = solved["rgba"][:, :, :3].astype(np.int16)
    magenta = (result_bgr[:, :, 0] > result_bgr[:, :, 1] + 2) & (
        result_bgr[:, :, 2] > result_bgr[:, :, 1] + 2
    )
    assert not np.any(magenta)
    assert int(np.max(np.abs(result_bgr - image.astype(np.int16)))) <= 1


def test_manual_despill_matches_preview_on_low_support_glow() -> None:
    image = np.full((12, 16, 3), (18, 180, 12), dtype=np.uint8)
    plate = np.full((12, 16, 3), (0, 170, 0), dtype=np.uint8)
    alpha = np.full((12, 16), 128, dtype=np.uint8)
    corrected = _conservative_manual_despill_bgr(
        image, alpha, plate, np.ones((12, 16), dtype=np.float32)
    )

    assert np.array_equal(corrected[:, :, [0, 2]], image[:, :, [0, 2]])
    assert np.all(corrected[:, :, 1] == 146)


def test_manual_despill_never_raises_a_clean_subject_channel() -> None:
    source = np.array([[[25, 35, 225]]], dtype=np.uint8)
    plate = np.array([[[0, 220, 0]]], dtype=np.uint8)
    alpha = np.array([[255]], dtype=np.uint8)
    corrected = _conservative_manual_despill_bgr(
        source, alpha, plate, np.ones((1, 1), dtype=np.float32)
    )

    assert np.array_equal(corrected, source)


def test_manual_despill_supports_two_channel_magenta_screen() -> None:
    plate = np.full((8, 10, 3), (255, 0, 255), dtype=np.uint8)
    alpha = np.full((8, 10), 128, dtype=np.uint8)
    pure_screen = plate.copy()
    assert np.array_equal(
        _conservative_manual_despill_bgr(
            pure_screen,
            alpha,
            plate,
            np.ones(alpha.shape, dtype=np.float32),
        ),
        pure_screen,
    )

    contaminated = np.full((8, 10, 3), (220, 80, 210), dtype=np.uint8)
    corrected = _conservative_manual_despill_bgr(
        contaminated,
        alpha,
        plate,
        np.ones(alpha.shape, dtype=np.float32),
    )
    assert np.all(corrected[:, :, 0] == 80)
    assert np.all(corrected[:, :, 1] == 80)
    assert np.all(corrected[:, :, 2] == 80)


def test_manual_despill_preserves_reliable_subject_core() -> None:
    plate = np.full((4, 4, 3), (255, 0, 255), dtype=np.uint8)
    contaminated = np.full((4, 4, 3), (220, 80, 210), dtype=np.uint8)
    subject_core = np.zeros((4, 4), dtype=bool)
    subject_core[1, 1] = True

    corrected = _conservative_manual_despill_bgr(
        contaminated,
        np.full((4, 4), 128, dtype=np.uint8),
        plate,
        np.ones((4, 4), dtype=np.float32),
        subject_core=subject_core,
    )

    assert np.array_equal(corrected[1, 1], contaminated[1, 1])
    assert np.array_equal(corrected[0, 0], np.array([80, 80, 80], dtype=np.uint8))


def test_shared_screen_color_golden_vectors() -> None:
    golden_path = (
        Path(__file__).parents[2]
        / "tests"
        / "fixtures"
        / "screen-color-golden.json"
    )
    cases = json.loads(golden_path.read_text(encoding="utf-8"))
    for item in cases:
        source_rgb = np.array([[item["sourceRgb"]]], dtype=np.uint8)
        screen_rgb = np.array([[item["screenRgb"]]], dtype=np.uint8)
        result_bgr = _conservative_manual_despill_bgr(
            source_rgb[:, :, ::-1],
            np.full((1, 1), 255, dtype=np.uint8),
            screen_rgb[:, :, ::-1],
            np.ones((1, 1), dtype=np.float32),
        )
        expected = np.array([[item["expectedRgb"]]], dtype=np.int16)
        assert np.max(
            np.abs(result_bgr[:, :, ::-1].astype(np.int16) - expected)
        ) <= 1, item["name"]


def test_chroma_key_removes_magenta_screen_without_green_assumption() -> None:
    image = np.full((64, 80, 3), (235, 10, 235), dtype=np.uint8)
    cv2.rectangle(image, (24, 8), (56, 58), (35, 205, 45), thickness=-1)
    rgba, alpha, qc = chroma_rgba(
        image,
        {
            "screen_samples": [{"rgb": [235, 10, 235]}],
            "threshold_low": 12,
            "threshold_high": 48,
            "spill_strength": 0.8,
        },
    )

    assert int(np.max(alpha[:4])) == 0
    assert int(np.median(alpha[16:50, 30:50])) > 240
    assert qc["screen_residue_ratio"] >= 0.0
    assert "screen_residue" in qc
    assert np.array_equal(rgba[:, :, 3], alpha)


def test_symmetric_temporal_fusion_reduces_stable_soft_alpha_flicker() -> None:
    _, plate, foreground, expected_alpha, options = _synthetic_translucent_screen()
    rng = np.random.default_rng(7)
    local_results: list[tuple[np.ndarray, np.ndarray, dict[str, object]]] = []
    for index in range(5):
        ai_alpha = np.clip(
            expected_alpha
            + rng.normal(0, 0.10, expected_alpha.shape)
            * ((expected_alpha > 0.02) & (expected_alpha < 0.98)),
            0,
            1,
        )
        shifted_plate = np.clip(
            plate.astype(np.int16)
            + (index - 2) * np.array([1, 3, 1], dtype=np.int16),
            0,
            255,
        ).astype(np.uint8)
        frame = _test_to_srgb(
            _test_to_linear(foreground) * expected_alpha[:, :, None]
            + _test_to_linear(shifted_plate)
            * (1.0 - expected_alpha[:, :, None])
        )
        ai_u8 = np.clip(ai_alpha * 255.0, 0, 255).astype(np.uint8)
        local_results.append(
            (
                frame,
                ai_u8,
                _prepare_and_solve(
                    frame,
                    options,
                    ai_alpha=ai_u8,
                    source_timeline_ordinal=0,
                ),
            )
        )

    final_results: list[dict[str, object]] = []
    for index, (frame, ai_u8, _) in enumerate(local_results):
        neighbors = [
            local_results[item][2]
            for item in (index - 1, index + 1)
            if 0 <= item < len(local_results)
        ]
        final_results.append(
            _prepare_and_solve(
                frame,
                options,
                ai_alpha=ai_u8,
                temporal_alpha=np.mean(
                    [item["alpha_float"] for item in neighbors], axis=0
                ),
                temporal_premultiplied=np.mean(
                    [item["premultiplied"] for item in neighbors], axis=0
                ),
                temporal_confidence=np.ones_like(expected_alpha),
                source_timeline_ordinal=0,
            )
        )

    unknown = (expected_alpha > 0.03) & (expected_alpha < 0.95)
    local_stack = np.stack(
        [item[2]["alpha_float"] for item in local_results]
    )
    final_stack = np.stack([item["alpha_float"] for item in final_results])
    local_flicker = float(np.mean(np.std(local_stack[:, unknown], axis=0)))
    final_flicker = float(np.mean(np.std(final_stack[:, unknown], axis=0)))
    assert final_flicker <= local_flicker * 0.70


def test_preserve_subject_screen_color_uses_semantic_connected_core() -> None:
    image = np.full((96, 128, 3), (0, 220, 0), dtype=np.uint8)
    cv2.rectangle(image, (34, 12), (94, 88), (30, 30, 220), thickness=-1)
    cv2.rectangle(image, (52, 35), (76, 66), (0, 220, 0), thickness=-1)
    ai_alpha = np.zeros((96, 128), dtype=np.uint8)
    cv2.rectangle(ai_alpha, (34, 12), (94, 88), 255, thickness=-1)
    common = {
        "screen_samples": [{"rgb": [0, 220, 0]}],
        "threshold_low": 12,
        "threshold_high": 48,
        "cleanup_radius": 1,
        "feather": 1,
        "spill_strength": 0.72,
    }

    clean = _prepare_and_solve(
        image, {**common, "key_mode": "clean_screen"}, ai_alpha=ai_alpha
    )
    protected = _prepare_and_solve(
        image,
        {**common, "key_mode": "preserve_subject_screen_color"},
        ai_alpha=ai_alpha,
    )

    assert int(protected["alpha"][50, 64]) > 245
    assert int(clean["alpha"][50, 64]) < int(protected["alpha"][50, 64]) - 50
    assert int(protected["alpha"][4, 4]) < 5
    assert int(protected["rgba"][50, 64, 1]) > 200


def test_sequence_clean_plate_recovers_cells_hidden_in_other_frames() -> None:
    base = np.full((8, 8, 3), 0.42, dtype=np.float32)
    models: list[dict[str, object]] = []
    for index in range(3):
        grid = base.copy()
        confidence = np.full((8, 8), 0.72, dtype=np.float32)
        if index == 0:
            grid[3, 4] = [0.02, 0.05, 0.02]
            confidence[3, 4] = 0.05
        models.append(
            {
                "grid_linear": grid,
                "grid_bgr": _test_to_srgb(grid),
                "grid_confidence": confidence,
                "median_bgr": np.array([0, 180, 0], dtype=np.float32),
                "luminance": 105.0,
                "mean_confidence": float(np.mean(confidence)),
            }
        )

    stabilized = stabilize_screen_models(models)

    assert len(stabilized) == 3
    assert np.allclose(stabilized[0]["grid_linear"][3, 4], base[3, 4], atol=0.04)
    assert stabilized[0]["grid_confidence"][3, 4] > 0.7
    assert stabilized[0]["sequence_plate_frames"] == 3
    assert stabilized[0]["sequence_plate_support"] > 1.5


def test_sequence_clean_plate_does_not_inflate_unreliable_or_single_frame_cells() -> None:
    grid = np.full((8, 8, 3), 0.42, dtype=np.float32)
    low_confidence_models = [
        {
            "grid_linear": grid.copy(),
            "grid_bgr": _test_to_srgb(grid),
            "grid_confidence": np.full((8, 8), 0.05, dtype=np.float32),
            "median_bgr": np.array([0, 180, 0], dtype=np.float32),
            "luminance": 105.0,
            "mean_confidence": 0.05,
        }
        for _ in range(100)
    ]

    stabilized = stabilize_screen_models(low_confidence_models)

    assert max(float(np.max(item["grid_confidence"])) for item in stabilized) <= 0.051
    assert max(float(item["sequence_plate_confidence"]) for item in stabilized) == 0.0
    single = stabilize_screen_models([low_confidence_models[0]])
    assert single[0]["grid_confidence"][0, 0] == pytest.approx(0.05)


def test_hybrid_removes_detached_screen_green_but_keeps_coloured_effect() -> None:
    image = np.full((112, 144, 3), (0, 220, 0), dtype=np.uint8)
    cv2.rectangle(image, (56, 18), (112, 101), (30, 30, 220), thickness=-1)
    cv2.circle(image, (25, 48), 12, (20, 200, 0), thickness=-1)
    cv2.circle(image, (130, 48), 8, (210, 50, 190), thickness=-1)
    ai_alpha = np.zeros((112, 144), dtype=np.uint8)
    cv2.rectangle(ai_alpha, (56, 18), (112, 101), 255, thickness=-1)
    cv2.circle(ai_alpha, (25, 48), 12, 250, thickness=-1)
    cv2.circle(ai_alpha, (130, 48), 8, 235, thickness=-1)
    options = {
        "screen_samples": [{"rgb": [0, 220, 0]}],
        "threshold_low": 12,
        "threshold_high": 52,
        "cleanup_radius": 1,
        "feather": 1,
        "spill_strength": 0.72,
        "key_mode": "preserve_subject_screen_color",
    }

    solved = _prepare_and_solve(image, options, ai_alpha=ai_alpha)

    assert int(solved["alpha"][60, 80]) > 245
    assert int(solved["alpha"][48, 25]) < 8
    assert int(solved["alpha"][48, 130]) > 160
    assert solved["qc"]["core_matte_ratio"] > 0
    assert solved["qc"]["effect_matte_ratio"] > 0
    assert solved["qc"]["effect_route_winner_ratio"] > 0
    assert solved["qc"]["garbage_components_removed"] >= 1




def test_location_aware_sample_pins_only_its_source_frame() -> None:
    image = np.full((96, 128, 3), (0, 210, 0), dtype=np.uint8)
    options = {
        "screen_samples": [
            {
                "rgb": [240, 15, 20],
                "x": 0.5,
                "y": 0.5,
                "source_timeline_ordinal": 7,
            },
            {
                "rgb": [0, 210, 0],
                "x": None,
                "y": None,
                "source_timeline_ordinal": None,
            },
        ],
        "threshold_low": 12,
        "threshold_high": 48,
    }
    matching = fit_screen_model(image, options, source_timeline_ordinal=7)
    other = fit_screen_model(image, options, source_timeline_ordinal=8)
    center = (
        int(0.5 * (matching["grid_bgr"].shape[0] - 1)),
        int(0.5 * (matching["grid_bgr"].shape[1] - 1)),
    )

    assert np.allclose(matching["grid_bgr"][center], [20, 15, 240], atol=1)
    assert matching["grid_confidence"][center] == 1.0
    assert not np.allclose(other["grid_bgr"][center], [20, 15, 240], atol=1)


