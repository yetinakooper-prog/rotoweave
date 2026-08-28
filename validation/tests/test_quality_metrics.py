from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate-matting-quality.py"
SPEC = importlib.util.spec_from_file_location("matting_quality_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)


def test_identical_rgba_has_zero_quality_error() -> None:
    truth = np.zeros((16, 16, 4), dtype=np.uint8)
    truth[3:13, 4:12] = (40, 80, 220, 255)
    core = truth[:, :, 3] > 200
    metrics = QUALITY.frame_metrics(truth.copy(), truth, [255, 0, 255], core)
    assert all(value == 0.0 for value in metrics.values())


def test_ultra_quality_gate_enforces_improvement_regression_and_core_limits() -> None:
    high = {
        "alphaSad": 1.0,
        "alphaMse": 1.0,
        "alphaGradient": 1.0,
        "alphaConnectivity": 1.0,
        "foregroundRgb": 1.0,
        "screenResidue": 1.0,
        "temporalFlicker": 1.0,
        "subjectDamage": 0.0,
    }
    ultra = {
        **high,
        "alphaSad": 0.8,
        "alphaMse": 0.85,
        "temporalFlicker": 0.8,
        "screenResidue": 0.9,
        "subjectDamage": 0.001,
    }
    passed = QUALITY.gate(high, ultra)
    assert passed["passed"] is True
    assert passed["hardCaseQualityGainRatio"] == pytest.approx(0.15)
    ultra["foregroundRgb"] = 1.021
    assert QUALITY.gate(high, ultra)["passed"] is False
    ultra["foregroundRgb"] = 1.0
    ultra["subjectDamage"] = 0.0011
    assert QUALITY.gate(high, ultra)["passed"] is False
