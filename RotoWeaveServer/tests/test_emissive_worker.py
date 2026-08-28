from __future__ import annotations

import numpy as np
import pytest

from worker.cuda_matting.__main__ import (
    _emission_clipping_statistics,
    _is_destructively_clipped,
    _validate_emission_sequence_energy,
    _validated_emission_energy,
)


def test_validated_black_source_keeps_dim_particle_energy_exactly() -> None:
    source = np.zeros((64, 64, 3), dtype=np.float32)
    source[31, 32] = (0.004, 0.002, 0.001)
    source[30:34, 30:34] += (0.08, 0.02, 0.12)

    emission, stats = _validated_emission_energy(source)

    assert np.array_equal(emission, source)
    assert stats["borderDarkRatio"] == 1.0
    assert stats["backgroundMax"] == 0.0


def test_black_source_gate_rejects_small_black_patch_on_gray_background() -> None:
    source = np.full((100, 100, 3), 0.5, dtype=np.float32)
    source[:10, :] = 0.0

    with pytest.raises(RuntimeError, match="uniformly black boundary"):
        _validated_emission_energy(source)


def test_black_source_gate_rejects_uniformly_elevated_black() -> None:
    source = np.full((64, 64, 3), 0.02, dtype=np.float32)

    with pytest.raises(RuntimeError, match="uniformly black boundary"):
        _validated_emission_energy(source)


def test_black_source_gate_allows_emission_to_cross_the_boundary() -> None:
    source = np.zeros((96, 96, 3), dtype=np.float32)
    source[:, :18] = (0.0, 1.0, 1.0)
    source[32:64, 18:70] = (0.0, 0.25, 0.4)

    emission, stats = _validated_emission_energy(source)

    assert np.array_equal(emission, source)
    assert stats["darkRatio"] >= 0.35
    assert stats["borderDarkRatio"] >= 0.65
    assert stats["borderP95Energy"] > 0.9
    assert stats["backgroundMax"] == 0.0


def test_black_source_gate_rejects_small_black_corner_in_non_black_scene() -> None:
    source = np.full((96, 96, 3), (0.08, 0.12, 0.16), dtype=np.float32)
    source[:24, :24] = 0.0

    with pytest.raises(RuntimeError, match="uniformly black boundary"):
        _validated_emission_energy(source)


def test_emission_clipping_allows_bounded_colored_channel_saturation() -> None:
    source = np.zeros((100, 100, 3), dtype=np.float32)
    source[25:75, 45:55] = (0.1, 1.0, 1.0)

    stats = _emission_clipping_statistics(source)

    assert stats["clippingRatio"] == pytest.approx(0.05)
    assert stats["luminanceClippingRatio"] == 0.0
    assert _is_destructively_clipped(stats) is False


def test_emission_clipping_rejects_large_clipped_region() -> None:
    source = np.zeros((100, 100, 3), dtype=np.float32)
    source[:50, :] = 1.0

    assert _is_destructively_clipped(_emission_clipping_statistics(source)) is True


def test_emissive_sequence_gate_rejects_an_entirely_empty_effect() -> None:
    with pytest.raises(RuntimeError, match="no usable emission energy"):
        _validate_emission_sequence_energy(0, 0.0)


def test_emissive_sequence_gate_allows_empty_frames_when_sequence_has_energy() -> None:
    _validate_emission_sequence_energy(1, 0.003)
