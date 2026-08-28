from __future__ import annotations

from copy import deepcopy

import pytest

from backend.app.mattebench import REQUIRED_CATEGORIES, MatteBenchError, aggregate


def passing_payload() -> dict:
    records = []
    routes = ("chroma_character", "emissive_vfx")
    for index, category in enumerate(REQUIRED_CATEGORIES):
        records.append(
            {
                "id": f"synthetic-{index}",
                "route": routes[index % len(routes)],
                "categories": [category],
                "sourceKind": "synthetic-ground-truth",
                "frameCount": 18,
                "metrics": {
                    "boundaryColorError": {"hybridV5": 1.0, "candidateV3": 0.60},
                    "screenResidue": {"hybridV5": 1.0, "candidateV3": 0.40},
                    "lowAlphaEmissionError": {"hybridV5": 1.0, "candidateV3": 0.60},
                    "temporalFlicker": {"hybridV5": 1.0, "candidateV3": 0.70},
                    "manualCorrectionMinutes": {"hybridV5": 10.0, "candidateV3": 4.0},
                    "subjectCoreDamage": {"hybridV5": 0.0005, "candidateV3": 0.0004},
                    "particleRecall": {"hybridV5": 0.80, "candidateV3": 0.90},
                },
            }
        )
    records.append(
        {
            "id": "blind-doubao-1",
            "route": "chroma_character",
            "categories": ["green_screen"],
            "sourceKind": "blind-real",
            "blindModelNames": True,
            "frameCount": 12,
            "metrics": {
                "manualCorrectionMinutes": {"hybridV5": 20.0, "candidateV3": 8.0}
            },
        }
    )
    return {"schemaVersion": 1, "benchmark": "MatteBench-v1", "records": records}


def test_mattebench_reports_routes_categories_percentiles_and_ci() -> None:
    report = aggregate(passing_payload(), resamples=100, seed=7)
    assert report["gate"]["passed"] is True
    assert report["syntheticFrameCount"] == 216
    assert set(report["groups"]["byRoute"]) == {
        "chroma_character",
        "emissive_vfx",
    }
    assert set(report["groups"]["byCategory"]) == set(REQUIRED_CATEGORIES)
    summary = report["groups"]["overall"]["metrics"]["screenResidue"]
    assert summary["candidateV3"]["median"] == pytest.approx(0.4)
    assert summary["candidateV3"]["p90"] == pytest.approx(0.4)
    assert len(summary["candidateV3"]["bootstrap95MedianCi"]) == 2


def test_mattebench_particle_regression_is_release_blocking() -> None:
    payload = deepcopy(passing_payload())
    payload["records"][0]["metrics"]["particleRecall"]["candidateV3"] = 0.79
    report = aggregate(payload, resamples=100)
    assert report["gate"]["passed"] is False
    assert "particle-recall-regression" in {
        item["code"] for item in report["gate"]["failures"]
    }


def test_mattebench_rejects_unblinded_real_record() -> None:
    payload = passing_payload()
    payload["records"][-1]["blindModelNames"] = False
    with pytest.raises(MatteBenchError, match="blindModelNames"):
        aggregate(payload, resamples=100)
