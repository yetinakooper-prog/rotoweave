from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

import numpy as np


MATTEBENCH_SCHEMA_VERSION = 1
MATTEBENCH_NAME = "MatteBench-v1"
PRODUCTION_ROUTES = ("chroma_character", "emissive_vfx")
REQUIRED_CATEGORIES = (
    "green_screen",
    "blue_screen",
    "hair",
    "fine_lines",
    "motion_blur",
    "translucent_material",
    "cyan_white_purple_emission",
    "smoke",
    "independent_particles",
    "green_subject",
    "compression",
    "clipping",
)
LOWER_IS_BETTER = {
    "boundaryColorError",
    "screenResidue",
    "lowAlphaEmissionError",
    "temporalFlicker",
    "manualCorrectionMinutes",
    "subjectCoreDamage",
}
HIGHER_IS_BETTER = {"particleRecall"}
METRICS = LOWER_IS_BETTER | HIGHER_IS_BETTER
IMPROVEMENT_THRESHOLDS = {
    "boundaryColorError": 0.30,
    "screenResidue": 0.50,
    "lowAlphaEmissionError": 0.30,
    "temporalFlicker": 0.20,
    "manualCorrectionMinutes": 0.50,
}
GATE_CATEGORY_SCOPE = {
    "boundaryColorError": {
        "green_screen",
        "blue_screen",
        "hair",
        "fine_lines",
        "motion_blur",
        "translucent_material",
        "green_subject",
        "compression",
        "clipping",
    },
    "screenResidue": {"green_screen", "blue_screen", "green_subject", "compression"},
    "lowAlphaEmissionError": {
        "cyan_white_purple_emission",
        "smoke",
        "independent_particles",
        "clipping",
    },
}


class MatteBenchError(RuntimeError):
    pass


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatteBenchError(f"{label} must be a number.")
    number = float(value)
    if not np.isfinite(number) or number < 0:
        raise MatteBenchError(f"{label} must be finite and non-negative.")
    return number


def validate_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schemaVersion") != MATTEBENCH_SCHEMA_VERSION:
        raise MatteBenchError("Unsupported MatteBench schemaVersion.")
    if payload.get("benchmark") != MATTEBENCH_NAME:
        raise MatteBenchError(f"benchmark must be {MATTEBENCH_NAME}.")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise MatteBenchError("MatteBench contains no records.")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            raise MatteBenchError(f"records[{index}] must be an object.")
        record_id = str(raw.get("id") or "").strip()
        if not record_id or record_id in seen:
            raise MatteBenchError("MatteBench record ids must be non-empty and unique.")
        seen.add(record_id)
        route = str(raw.get("route") or "")
        if route not in PRODUCTION_ROUTES:
            raise MatteBenchError(f"Record {record_id} has an invalid route.")
        categories = raw.get("categories")
        if (
            not isinstance(categories, list)
            or not categories
            or any(item not in REQUIRED_CATEGORIES for item in categories)
            or len(set(categories)) != len(categories)
        ):
            raise MatteBenchError(f"Record {record_id} has invalid categories.")
        source_kind = str(raw.get("sourceKind") or "")
        if source_kind not in {"synthetic-ground-truth", "blind-real"}:
            raise MatteBenchError(f"Record {record_id} has an invalid sourceKind.")
        if source_kind == "blind-real" and raw.get("blindModelNames") is not True:
            raise MatteBenchError(
                f"Blind-real record {record_id} must set blindModelNames=true."
            )
        frame_count = raw.get("frameCount")
        if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count < 1:
            raise MatteBenchError(f"Record {record_id} has an invalid frameCount.")
        raw_metrics = raw.get("metrics")
        if not isinstance(raw_metrics, dict) or not raw_metrics:
            raise MatteBenchError(f"Record {record_id} has no metrics.")
        metrics: dict[str, dict[str, float]] = {}
        for name, pair in raw_metrics.items():
            if name not in METRICS:
                raise MatteBenchError(f"Record {record_id} uses unknown metric {name}.")
            if not isinstance(pair, dict):
                raise MatteBenchError(f"Metric {name} in {record_id} must be an object.")
            baseline = _finite_number(pair.get("hybridV5"), f"{record_id}.{name}.hybridV5")
            candidate = _finite_number(pair.get("candidateV3"), f"{record_id}.{name}.candidateV3")
            if name in {"subjectCoreDamage", "particleRecall"} and (
                baseline > 1.0 or candidate > 1.0
            ):
                raise MatteBenchError(f"Metric {name} in {record_id} must be in [0, 1].")
            metrics[name] = {"hybridV5": baseline, "candidateV3": candidate}
        records.append(
            {
                "id": record_id,
                "route": route,
                "categories": list(categories),
                "sourceKind": source_kind,
                "blindModelNames": bool(raw.get("blindModelNames", False)),
                "frameCount": frame_count,
                "metrics": metrics,
            }
        )
    return records


def _improvement(metric: str, baseline: float, candidate: float) -> float:
    if baseline <= 1e-12:
        if candidate <= 1e-12:
            return 0.0
        return -1.0 if metric in LOWER_IS_BETTER else 1.0
    if metric in LOWER_IS_BETTER:
        return (baseline - candidate) / baseline
    return (candidate - baseline) / baseline


def _bootstrap_median_ci(
    values: np.ndarray, *, resamples: int, rng: np.random.Generator
) -> list[float]:
    if values.size == 1:
        value = float(values[0])
        return [value, value]
    indices = rng.integers(0, values.size, size=(resamples, values.size))
    medians = np.median(values[indices], axis=1)
    return [float(np.percentile(medians, 2.5)), float(np.percentile(medians, 97.5))]


def _summary(
    values: Iterable[float], *, resamples: int, rng: np.random.Generator
) -> dict[str, Any]:
    array = np.asarray(list(values), dtype=np.float64)
    return {
        "count": int(array.size),
        "median": float(np.median(array)),
        "p90": float(np.percentile(array, 90)),
        "p95": float(np.percentile(array, 95)),
        "bootstrap95MedianCi": _bootstrap_median_ci(
            array, resamples=resamples, rng=rng
        ),
    }


def _aggregate_group(
    records: list[dict[str, Any]], *, resamples: int, rng: np.random.Generator
) -> dict[str, Any]:
    by_metric: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for record in records:
        for name, pair in record["metrics"].items():
            by_metric[name].append((pair["hybridV5"], pair["candidateV3"]))
    metrics: dict[str, Any] = {}
    for name in sorted(by_metric):
        pairs = by_metric[name]
        baseline = [item[0] for item in pairs]
        candidate = [item[1] for item in pairs]
        improvements = [_improvement(name, item[0], item[1]) for item in pairs]
        metrics[name] = {
            "hybridV5": _summary(baseline, resamples=resamples, rng=rng),
            "candidateV3": _summary(candidate, resamples=resamples, rng=rng),
            "improvementRatio": _summary(improvements, resamples=resamples, rng=rng),
        }
    return {
        "recordCount": len(records),
        "frameCount": sum(item["frameCount"] for item in records),
        "metrics": metrics,
    }


def aggregate(
    payload: dict[str, Any], *, resamples: int = 2000, seed: int = 4090
) -> dict[str, Any]:
    if not 100 <= resamples <= 100_000:
        raise MatteBenchError("Bootstrap resamples must be between 100 and 100000.")
    records = validate_payload(payload)
    rng = np.random.default_rng(seed)
    by_route = {
        route: _aggregate_group(
            [record for record in records if record["route"] == route],
            resamples=resamples,
            rng=rng,
        )
        for route in PRODUCTION_ROUTES
        if any(record["route"] == route for record in records)
    }
    by_category = {
        category: _aggregate_group(
            [record for record in records if category in record["categories"]],
            resamples=resamples,
            rng=rng,
        )
        for category in REQUIRED_CATEGORIES
        if any(category in record["categories"] for record in records)
    }
    overall = _aggregate_group(records, resamples=resamples, rng=rng)
    failures: list[dict[str, Any]] = []

    def require(check: bool, code: str, detail: str) -> None:
        if not check:
            failures.append({"code": code, "detail": detail})

    synthetic_frame_count = sum(
        record["frameCount"]
        for record in records
        if record["sourceKind"] == "synthetic-ground-truth"
    )
    blind_count = sum(record["sourceKind"] == "blind-real" for record in records)
    require(
        200 <= synthetic_frame_count <= 300,
        "synthetic-frame-count",
        f"Expected 200..300 synthetic frames, observed {synthetic_frame_count}.",
    )
    for route in PRODUCTION_ROUTES:
        require(route in by_route, "missing-route", f"No records for route {route}.")
    for category in REQUIRED_CATEGORIES:
        require(
            category in by_category,
            "missing-category",
            f"No records for category {category}.",
        )
    require(blind_count > 0, "missing-blind-real", "No blind-real source evaluation exists.")
    for metric in METRICS:
        require(
            metric in overall["metrics"],
            "missing-metric",
            f"No paired samples for metric {metric}.",
        )
    for route in PRODUCTION_ROUTES:
        require(
            any(
                record["route"] == route and "subjectCoreDamage" in record["metrics"]
                for record in records
            ),
            "missing-subject-protection-evidence",
            f"Route {route} has no subject-core protection measurement.",
        )
    require(
        any(
            "independent_particles" in record["categories"]
            and "particleRecall" in record["metrics"]
            for record in records
        ),
        "missing-particle-evidence",
        "The independent-particles category has no recall measurement.",
    )
    gate_metric_evidence: dict[str, Any] = {}
    for metric, threshold in IMPROVEMENT_THRESHOLDS.items():
        if metric == "manualCorrectionMinutes":
            scoped = [record for record in records if record["sourceKind"] == "blind-real"]
        elif metric == "temporalFlicker":
            scoped = [record for record in records if record["frameCount"] > 1]
            for route in PRODUCTION_ROUTES:
                require(
                    any(
                        record["route"] == route
                        and record["frameCount"] > 1
                        and metric in record["metrics"]
                        for record in records
                    ),
                    "missing-gate-evidence",
                    f"Route {route} has no temporal-flicker evidence.",
                )
        else:
            categories = GATE_CATEGORY_SCOPE[metric]
            scoped = [
                record
                for record in records
                if categories.intersection(record["categories"])
            ]
            for category in sorted(categories):
                require(
                    any(
                        category in record["categories"]
                        and metric in record["metrics"]
                        for record in records
                    ),
                    "missing-gate-evidence",
                    f"Category {category} has no {metric} evidence.",
                )
        pairs = [
            record["metrics"][metric]
            for record in scoped
            if metric in record["metrics"]
        ]
        require(
            bool(pairs),
            "missing-gate-evidence",
            f"No scoped hard-gate evidence for {metric}.",
        )
        if pairs:
            values = [
                _improvement(metric, pair["hybridV5"], pair["candidateV3"])
                for pair in pairs
            ]
            value = float(np.median(values))
            gate_metric_evidence[metric] = {
                "pairedRecordCount": len(pairs),
                "medianImprovementRatio": value,
                "requiredImprovementRatio": threshold,
            }
            require(
                value >= threshold,
                "improvement-threshold",
                f"{metric} median improvement {value:.6f} is below {threshold:.2%}.",
            )

    subject_pairs = [
        pair
        for record in records
        for name, pair in record["metrics"].items()
        if name == "subjectCoreDamage"
    ]
    if subject_pairs:
        max_damage = max(item["candidateV3"] for item in subject_pairs)
        regressions = sum(
            item["candidateV3"] > item["hybridV5"] + 1e-12 for item in subject_pairs
        )
        require(
            max_damage <= 0.001,
            "subject-core-damage",
            f"Maximum subject-core damage {max_damage:.6f} exceeds 0.1%.",
        )
        require(
            regressions == 0,
            "subject-core-regression",
            f"Subject-core damage regressed in {regressions} records.",
        )
    particle_pairs = [
        pair
        for record in records
        for name, pair in record["metrics"].items()
        if name == "particleRecall"
    ]
    if particle_pairs:
        regressions = sum(
            item["candidateV3"] + 1e-12 < item["hybridV5"] for item in particle_pairs
        )
        require(
            regressions == 0,
            "particle-recall-regression",
            f"Independent-particle recall regressed in {regressions} records.",
        )

    for dimension, groups in (("route", by_route), ("category", by_category)):
        for group_name, summary in groups.items():
            for metric, metric_summary in summary["metrics"].items():
                improvement = metric_summary["improvementRatio"]["median"]
                require(
                    improvement >= -0.02,
                    "group-regression",
                    f"{dimension}={group_name}, metric={metric} regressed "
                    f"{abs(improvement):.2%}, over the 2% limit.",
                )

    return {
        "schemaVersion": MATTEBENCH_SCHEMA_VERSION,
        "benchmark": MATTEBENCH_NAME,
        "recordCount": len(records),
        "syntheticFrameCount": synthetic_frame_count,
        "blindRealRecordCount": blind_count,
        "bootstrap": {"resamples": resamples, "seed": seed, "confidence": 0.95},
        "groups": {
            "overall": overall,
            "byRoute": by_route,
            "byCategory": by_category,
        },
        "gate": {
            "passed": not failures,
            "metricEvidence": gate_metric_evidence,
            "failures": failures,
        },
    }
