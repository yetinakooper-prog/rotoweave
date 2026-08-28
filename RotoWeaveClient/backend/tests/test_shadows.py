from __future__ import annotations

import numpy as np
import pytest

from backend.app.shadows import (
    measure_shadow_alpha,
    validate_light_angle,
    resolve_shadow_sequence,
)


def _alpha(kind: str) -> np.ndarray:
    alpha = np.zeros((80, 100), dtype=np.uint8)
    if kind == "biped":
        alpha[16:55, 34:66] = 255
        alpha[52:68, 35:43] = 255
        alpha[52:68, 57:65] = 255
    elif kind == "quadruped":
        alpha[25:52, 20:80] = 255
        for left in (22, 38, 62, 76):
            alpha[50:68, left : left + 5] = 255
    elif kind == "multileg":
        alpha[22:50, 18:82] = 255
        for left in range(20, 81, 10):
            alpha[48:68, left : left + 3] = 255
    elif kind == "continuous":
        alpha[42:67, 10:90] = 255
    else:
        raise AssertionError(kind)
    return alpha


def _resolve(
    alpha: np.ndarray,
    angle: float,
    *,
    pivot: tuple[float, float] = (50.0, 68.0),
    mode: str = "grounded",
    opacity: float = 0.35,
) -> dict:
    measurement = measure_shadow_alpha(
        alpha,
        pivot,
        light_angle_degrees=angle,
    )
    return resolve_shadow_sequence(
        [measurement],
        mode=mode,
        loop=False,
        opacity=opacity,
        light_angle_degrees=angle,
    )[0]


@pytest.mark.parametrize("kind", ["biped", "quadruped", "multileg", "continuous"])
def test_non_humanoid_contact_shapes_resolve_one_wide_flat_shadow(kind: str) -> None:
    resolved = _resolve(_alpha(kind), 135.0, mode="auto")

    assert resolved["widthPx"] > resolved["depthPx"] * 4
    assert resolved["depthPx"] >= 1.0
    assert resolved["rotationDegrees"] == 0.0
    assert 0.0 < resolved["alpha"] <= 0.35


def test_art_angles_only_extend_the_horizontal_far_side() -> None:
    alpha = _alpha("biped")
    right = _resolve(alpha, 45.0)
    centered = _resolve(alpha, 90.0)
    left = _resolve(alpha, 135.0)

    assert right["positionPx"][0] > centered["positionPx"][0] > left["positionPx"][0]
    assert right["widthPx"] == pytest.approx(left["widthPx"])
    assert right["widthPx"] > centered["widthPx"]
    assert {right["rotationDegrees"], centered["rotationDegrees"], left["rotationDegrees"]} == {0.0}
    assert right["depthPx"] == pytest.approx(left["depthPx"])
    assert right["depthPx"] < centered["depthPx"]
    assert right["positionPx"][1] == pytest.approx(left["positionPx"][1])
    assert abs(right["positionPx"][1]) < abs(centered["positionPx"][1])
    assert right["alpha"] == pytest.approx(left["alpha"])
    assert right["alpha"] < centered["alpha"]

    centered_left_edge = centered["positionPx"][0] - centered["widthPx"] / 2
    centered_right_edge = centered["positionPx"][0] + centered["widthPx"] / 2
    assert right["positionPx"][0] - right["widthPx"] / 2 == pytest.approx(
        centered_left_edge
    )
    assert left["positionPx"][0] + left["widthPx"] / 2 == pytest.approx(
        centered_right_edge
    )


def test_ground_line_controls_shadow_y_while_texture_y_only_changes_clearance() -> None:
    alpha = _alpha("biped")
    grounded = measure_shadow_alpha(
        alpha,
        (50.0, 68.0),
        ground_relative_down_px=12.0,
    )
    lifted = measure_shadow_alpha(
        alpha,
        (50.0, 68.0),
        offset_px=(0.0, -10.0),
        ground_relative_down_px=12.0,
    )
    assert grounded is not None and lifted is not None
    resolved_grounded = resolve_shadow_sequence(
        [grounded], mode="auto", loop=False, opacity=0.4, light_angle_degrees=90
    )[0]
    resolved_lifted = resolve_shadow_sequence(
        [lifted], mode="auto", loop=False, opacity=0.4, light_angle_degrees=90
    )[0]

    assert lifted["airbornePx"] > grounded["airbornePx"]
    assert resolved_lifted["positionPx"][1] < -12.0
    assert resolved_grounded["positionPx"][1] < -12.0
    assert resolved_lifted["alpha"] < resolved_grounded["alpha"]


def test_texture_scale_does_not_move_the_global_ground_line() -> None:
    alpha = _alpha("biped")
    normal = measure_shadow_alpha(
        alpha,
        (50.0, 68.0),
        scale=1.0,
        ground_relative_down_px=12.0,
    )
    scaled = measure_shadow_alpha(
        alpha,
        (50.0, 68.0),
        scale=1.5,
        ground_relative_down_px=12.0,
    )

    assert normal is not None and scaled is not None
    assert normal["groundPositionY"] == pytest.approx(-12.0)
    assert scaled["groundPositionY"] == pytest.approx(-12.0)
    assert scaled["bodyWidth"] > normal["bodyWidth"]


def test_content_below_shadow_axis_does_not_widen_or_shift_shadow() -> None:
    above_axis = np.zeros((120, 120), dtype=np.uint8)
    above_axis[20:81, 45:76] = 255
    with_below_axis = above_axis.copy()
    with_below_axis[80:86, 59:62] = 255  # Keep the lower content connected.
    with_below_axis[85:105, 5:116] = 255
    measurement_options = {
        "pivot_px": (60.0, 100.0),
        "scale": 1.5,
        "offset_px": (4.0, 7.0),
        # Source row 80 maps to the shadow axis after scale and frame offset.
        "ground_relative_down_px": -23.0,
        "light_angle_degrees": 45.0,
    }

    clean_measurement = measure_shadow_alpha(above_axis, **measurement_options)
    below_measurement = measure_shadow_alpha(with_below_axis, **measurement_options)

    assert clean_measurement is not None and below_measurement is not None
    assert below_measurement["bodyWidth"] == pytest.approx(
        clean_measurement["bodyWidth"]
    )
    assert below_measurement["contactSpan"] == pytest.approx(
        clean_measurement["contactSpan"]
    )
    assert below_measurement["projectionExtension"] == pytest.approx(
        clean_measurement["projectionExtension"]
    )

    clean_shadow = resolve_shadow_sequence(
        [clean_measurement],
        mode="grounded",
        loop=False,
        opacity=0.35,
        light_angle_degrees=45.0,
    )[0]
    below_shadow = resolve_shadow_sequence(
        [below_measurement],
        mode="grounded",
        loop=False,
        opacity=0.35,
        light_angle_degrees=45.0,
    )[0]
    assert below_shadow["widthPx"] == pytest.approx(clean_shadow["widthPx"])
    assert below_shadow["positionPx"] == pytest.approx(clean_shadow["positionPx"])


def test_zero_and_180_degree_projection_is_mirrored_and_capped() -> None:
    alpha = _alpha("biped")
    maximum_right = _resolve(alpha, 0.0)
    overhead = _resolve(alpha, 90.0)
    maximum_left = _resolve(alpha, 180.0)
    measurement = measure_shadow_alpha(alpha, (50.0, 68.0), light_angle_degrees=0.0)

    assert measurement is not None
    assert maximum_right["widthPx"] == pytest.approx(maximum_left["widthPx"])
    assert maximum_right["positionPx"][0] - overhead["positionPx"][0] == pytest.approx(
        overhead["positionPx"][0] - maximum_left["positionPx"][0]
    )
    assert maximum_right["widthPx"] <= (
        overhead["widthPx"] + measurement["bodyWidth"] * 0.65 + 1e-5
    )
    assert validate_light_angle(0.0) == 0.0
    assert validate_light_angle(180.0) == 180.0
    with pytest.raises(ValueError, match="0 到 180"):
        validate_light_angle(225.0)
    with pytest.raises(ValueError, match="0 到 180"):
        validate_light_angle(315.0)


def test_weapon_tail_fragment_and_single_pixel_noise_do_not_distort_projection() -> None:
    clean = _alpha("biped")
    noisy = clean.copy()
    noisy[18:56, 66] = 255  # Attached one-pixel weapon.
    noisy[64:67, 3:6] = 255  # Detached tail fragment near the support band.
    noisy[75, 98] = 255

    clean_shadow = _resolve(clean, 45.0)
    noisy_shadow = _resolve(noisy, 45.0)
    clean_measurement = measure_shadow_alpha(clean, (50.0, 68.0), light_angle_degrees=45.0)
    noisy_measurement = measure_shadow_alpha(noisy, (50.0, 68.0), light_angle_degrees=45.0)

    assert clean_measurement is not None and noisy_measurement is not None
    assert noisy_measurement["contactCenterX"] == pytest.approx(
        clean_measurement["contactCenterX"], abs=0.5
    )
    assert noisy_shadow["widthPx"] == pytest.approx(clean_shadow["widthPx"], abs=1.0)
    assert noisy_shadow["positionPx"][0] == pytest.approx(
        clean_shadow["positionPx"][0], abs=0.5
    )


def test_far_two_percent_artifact_cannot_become_the_contact_anchor() -> None:
    clean = np.zeros((240, 240), dtype=np.uint8)
    clean[35:190, 75:165] = 255
    clean[180:215, 85:105] = 255
    clean[180:215, 135:155] = 255
    artifact = clean.copy()
    artifact[210:225, 215:230] = 255

    clean_measurement = measure_shadow_alpha(
        clean,
        (120.0, 220.0),
        light_angle_degrees=135.0,
    )
    artifact_measurement = measure_shadow_alpha(
        artifact,
        (120.0, 220.0),
        light_angle_degrees=135.0,
    )
    assert clean_measurement is not None and artifact_measurement is not None
    assert artifact_measurement["contactCenterX"] == pytest.approx(
        clean_measurement["contactCenterX"], abs=0.5
    )
    assert artifact_measurement["bodyWidth"] == pytest.approx(
        clean_measurement["bodyWidth"], abs=0.5
    )
    assert artifact_measurement["projectionExtension"] == pytest.approx(
        clean_measurement["projectionExtension"], abs=0.5
    )


def test_material_alpha_reduces_shadow_depth_and_opacity_without_moving_contact() -> None:
    opaque = _alpha("biped")
    translucent = (opaque.astype(np.float64) * 0.25).astype(np.uint8)

    opaque_shadow = _resolve(opaque, 90.0, opacity=0.4)
    translucent_shadow = _resolve(translucent, 90.0, opacity=0.4)

    assert translucent_shadow["positionPx"][0] == pytest.approx(
        opaque_shadow["positionPx"][0]
    )
    assert translucent_shadow["widthPx"] == pytest.approx(opaque_shadow["widthPx"])
    assert translucent_shadow["depthPx"] < opaque_shadow["depthPx"]
    assert translucent_shadow["alpha"] < opaque_shadow["alpha"] * 0.75


def test_clearance_not_character_height_controls_size_and_alpha() -> None:
    grounded_short = np.zeros((100, 100), dtype=np.uint8)
    grounded_short[40:80, 30:70] = 255
    grounded_tall = np.zeros((100, 100), dtype=np.uint8)
    grounded_tall[5:80, 30:70] = 255
    airborne = np.zeros((100, 100), dtype=np.uint8)
    airborne[10:50, 30:70] = 255

    short_shadow = _resolve(grounded_short, 90.0, pivot=(50.0, 80.0), mode="auto", opacity=0.4)
    tall_shadow = _resolve(grounded_tall, 90.0, pivot=(50.0, 80.0), mode="auto", opacity=0.4)
    airborne_shadow = _resolve(airborne, 90.0, pivot=(50.0, 80.0), mode="auto", opacity=0.4)
    forced_grounded = _resolve(airborne, 90.0, pivot=(50.0, 80.0), mode="grounded", opacity=0.4)
    forced_flying = _resolve(grounded_short, 90.0, pivot=(50.0, 80.0), mode="flying", opacity=0.4)

    assert short_shadow["airborneRatio"] == 0.0
    assert tall_shadow["airborneRatio"] == 0.0
    assert short_shadow["alpha"] == pytest.approx(0.4)
    assert tall_shadow["alpha"] == pytest.approx(0.4)
    assert airborne_shadow["airborneRatio"] > 0
    assert airborne_shadow["widthPx"] < forced_grounded["widthPx"]
    assert airborne_shadow["depthPx"] < forced_grounded["depthPx"]
    assert airborne_shadow["alpha"] < forced_grounded["alpha"]
    assert forced_flying["airborneRatio"] > 0
    assert forced_flying["alpha"] < 0.4
    assert airborne_shadow["positionPx"][1] < 0  # It remains on the pivot ground line.


def test_three_frame_median_smoothing_wraps_for_looping_animation() -> None:
    def measurement(center: float) -> dict[str, float]:
        return {
            "contactCenterX": center,
            "bodyCenterX": center,
            "contactSpan": 20.0,
            "bodyWidth": 30.0,
            "bodyHeight": 40.0,
            "airbornePx": 0.0,
            "projectionExtension": 0.0,
        }

    middle_spike = resolve_shadow_sequence(
        [measurement(0), measurement(100), measurement(0)],
        mode="grounded",
        loop=False,
        opacity=0.35,
        light_angle_degrees=90.0,
    )
    edge_spike = resolve_shadow_sequence(
        [measurement(100), measurement(0), measurement(0)],
        mode="grounded",
        loop=True,
        opacity=0.35,
        light_angle_degrees=90.0,
    )

    assert middle_spike[1]["positionPx"][0] == pytest.approx(
        middle_spike[0]["positionPx"][0]
    )
    assert edge_spike[0]["positionPx"][0] == pytest.approx(
        edge_spike[1]["positionPx"][0]
    )


def test_incomparable_scale_frame_keeps_its_own_anchor_and_projection() -> None:
    def measurement(
        center: float,
        width: float,
        height: float,
        extension: float,
    ) -> dict[str, float]:
        return {
            "contactCenterX": center,
            "bodyCenterX": center,
            "contactSpan": width * 0.65,
            "bodyWidth": width,
            "massWidth": width * 0.72,
            "silhouetteWidth": width,
            "bodyHeight": height,
            "airbornePx": 0.0,
            "groundPositionY": 0.0,
            "projectionExtension": extension,
            "fillRatio": 0.5,
            "meanAlpha": 1.0,
            "contactConfidence": 1.0,
            "casterOpacity": 1.0,
        }

    current = measurement(0.1, 27.0, 40.0, 9.8)
    single = resolve_shadow_sequence(
        [current],
        mode="auto",
        loop=False,
        opacity=0.35,
        light_angle_degrees=135.0,
    )[0]
    sequence = resolve_shadow_sequence(
        [
            measurement(-6.0, 263.0, 422.0, 121.0),
            current,
            measurement(295.0, 285.0, 490.0, 120.0),
        ],
        mode="auto",
        loop=False,
        opacity=0.35,
        light_angle_degrees=135.0,
    )[1]

    assert sequence["positionPx"] == pytest.approx(single["positionPx"])
    assert sequence["widthPx"] == pytest.approx(single["widthPx"])
    assert sequence["depthPx"] == pytest.approx(single["depthPx"])


def test_two_frame_loop_does_not_swap_neighbor_values() -> None:
    measurements = [
        {
            "contactCenterX": center,
            "bodyCenterX": center,
            "contactSpan": 20.0,
            "bodyWidth": 30.0,
            "bodyHeight": 40.0,
            "airbornePx": 0.0,
            "projectionExtension": 0.0,
        }
        for center in (0.0, 100.0)
    ]

    resolved = resolve_shadow_sequence(
        measurements,
        mode="grounded",
        loop=True,
        opacity=0.35,
        light_angle_degrees=90.0,
    )

    assert resolved[0]["positionPx"][0] < resolved[1]["positionPx"][0]


def test_manual_width_depth_and_xy_are_applied_after_automatic_result() -> None:
    measurement = measure_shadow_alpha(
        _alpha("biped"),
        (50.0, 68.0),
        light_angle_degrees=135.0,
    )
    base = resolve_shadow_sequence(
        [measurement],
        mode="grounded",
        loop=False,
        opacity=0.35,
        light_angle_degrees=135.0,
    )[0]
    adjusted = resolve_shadow_sequence(
        [measurement],
        mode="grounded",
        loop=False,
        opacity=0.35,
        light_angle_degrees=135.0,
        adjustments=[
            {"widthScale": 2.0, "depthScale": 1.0, "offsetX": 3.0, "offsetY": 4.0}
        ],
    )[0]

    assert adjusted["widthPx"] == pytest.approx(base["widthPx"] * 2.0)
    assert adjusted["depthPx"] == pytest.approx(base["depthPx"])
    assert adjusted["positionPx"][0] == pytest.approx(base["positionPx"][0] + 3.0)
    assert adjusted["positionPx"][1] == pytest.approx(base["positionPx"][1] + 4.0)
