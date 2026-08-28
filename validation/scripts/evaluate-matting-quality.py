from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np


ALPHA_METRICS = (
    "alphaSad",
    "alphaMse",
    "alphaGradient",
    "alphaConnectivity",
)
MAJOR_METRICS = (*ALPHA_METRICS, "foregroundRgb", "screenResidue", "temporalFlicker")
REQUIRED_CATEGORIES = {
    "green_screen", "blue_screen", "hair", "fine_lines", "motion_blur",
    "translucent_material", "cyan_white_purple_emission", "smoke",
    "independent_particles", "green_subject", "compression", "clipping",
}


def read_rgba(path: Path) -> np.ndarray:
    value = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if value is None or value.ndim != 3 or value.shape[2] != 4:
        raise RuntimeError(f"Expected RGBA PNG: {path}")
    return value


def read_mask(path: Path, shape: tuple[int, int]) -> np.ndarray:
    value = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if value is None or value.shape != shape:
        raise RuntimeError(f"Invalid subject-core mask: {path}")
    return value > 127


def largest_component(value: np.ndarray) -> np.ndarray:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        value.astype(np.uint8), 8
    )
    if count <= 1:
        return np.zeros(value.shape, dtype=bool)
    label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return labels == label


def connectivity_error(predicted: np.ndarray, truth: np.ndarray) -> float:
    levels = np.arange(0.0, 1.01, 0.1, dtype=np.float32)
    predicted_level = np.full(predicted.shape, -1.0, dtype=np.float32)
    truth_level = np.full(truth.shape, -1.0, dtype=np.float32)
    for level in levels:
        omega = largest_component((predicted >= level) & (truth >= level))
        pending_predicted = (predicted_level < 0) & ~omega
        pending_truth = (truth_level < 0) & ~omega
        predicted_level[pending_predicted] = max(0.0, float(level) - 0.1)
        truth_level[pending_truth] = max(0.0, float(level) - 0.1)
    predicted_level[predicted_level < 0] = 1.0
    truth_level[truth_level < 0] = 1.0

    def phi(alpha: np.ndarray, level: np.ndarray) -> np.ndarray:
        delta = alpha - level
        return np.where(delta >= 0.15, 1.0 - delta, 1.0)

    return float(np.mean(np.abs(phi(predicted, predicted_level) - phi(truth, truth_level))))


def screen_residue(
    rgba: np.ndarray,
    truth: np.ndarray,
    screen_rgb: list[int] | tuple[int, int, int],
    core: np.ndarray | None,
) -> float:
    rgb = rgba[:, :, [2, 1, 0]].astype(np.float32) / 255.0
    truth_rgb = truth[:, :, [2, 1, 0]].astype(np.float32) / 255.0
    alpha = rgba[:, :, 3].astype(np.float32) / 255.0
    screen = np.asarray(screen_rgb, dtype=np.float32) / 255.0
    chroma = np.maximum(screen - float(np.min(screen)), 0.0)
    peak = float(np.max(chroma))
    if peak <= 1e-6:
        return 0.0
    direction = chroma / peak
    aligned = np.min(
        np.where(direction > 0.05, rgb / np.maximum(direction, 0.05), np.inf),
        axis=2,
    )
    truth_aligned = np.min(
        np.where(
            direction > 0.05,
            truth_rgb / np.maximum(direction, 0.05),
            np.inf,
        ),
        axis=2,
    )
    edge = (alpha > 0.02) & (alpha < 0.98)
    if core is not None:
        edge &= ~core
    if not np.any(edge):
        return 0.0
    excess = np.maximum(aligned - truth_aligned, 0.0)
    return float(np.sum(excess[edge] * alpha[edge]) / np.sum(alpha[edge]))


def frame_metrics(
    candidate: np.ndarray,
    truth: np.ndarray,
    screen_rgb: list[int],
    core: np.ndarray | None,
) -> dict[str, float]:
    if candidate.shape != truth.shape:
        raise RuntimeError("Candidate and truth dimensions differ.")
    predicted_alpha = candidate[:, :, 3].astype(np.float32) / 255.0
    truth_alpha = truth[:, :, 3].astype(np.float32) / 255.0
    difference = predicted_alpha - truth_alpha
    predicted_gradient_x = cv2.Sobel(predicted_alpha, cv2.CV_32F, 1, 0, ksize=3)
    predicted_gradient_y = cv2.Sobel(predicted_alpha, cv2.CV_32F, 0, 1, ksize=3)
    truth_gradient_x = cv2.Sobel(truth_alpha, cv2.CV_32F, 1, 0, ksize=3)
    truth_gradient_y = cv2.Sobel(truth_alpha, cv2.CV_32F, 0, 1, ksize=3)
    visible = truth_alpha > 0.01
    candidate_rgb = candidate[:, :, :3].astype(np.float32) / 255.0
    truth_rgb = truth[:, :, :3].astype(np.float32) / 255.0
    foreground_rgb = (
        float(
            np.sum(np.abs(candidate_rgb - truth_rgb) * truth_alpha[:, :, None])
            / max(3.0 * float(np.sum(truth_alpha)), 1.0)
        )
        if np.any(visible)
        else 0.0
    )
    affected = 0.0
    if core is not None and np.any(core):
        changed = (
            np.abs(candidate[:, :, 3].astype(np.int16) - truth[:, :, 3].astype(np.int16)) > 1
        ) | np.any(
            np.abs(candidate[:, :, :3].astype(np.int16) - truth[:, :, :3].astype(np.int16)) > 1,
            axis=2,
        )
        affected = float(np.count_nonzero(changed & core) / np.count_nonzero(core))
    return {
        "alphaSad": float(np.mean(np.abs(difference))),
        "alphaMse": float(np.mean(np.square(difference))),
        "alphaGradient": float(
            np.mean(
                np.sqrt(
                    np.square(predicted_gradient_x - truth_gradient_x)
                    + np.square(predicted_gradient_y - truth_gradient_y)
                )
            )
        ),
        "alphaConnectivity": connectivity_error(predicted_alpha, truth_alpha),
        "foregroundRgb": foreground_rgb,
        "screenResidue": screen_residue(candidate, truth, screen_rgb, core),
        "subjectDamage": affected,
    }


def evaluate_case(root: Path, case: dict[str, Any], profile: str) -> dict[str, float]:
    frame_results: list[dict[str, float]] = []
    alpha_errors: list[np.ndarray] = []
    for frame in case.get("frames") or []:
        truth = read_rgba(root / str(frame["truth"]))
        candidate = read_rgba(root / str(frame[profile]))
        core = (
            read_mask(root / str(frame["subjectCore"]), truth.shape[:2])
            if frame.get("subjectCore")
            else None
        )
        frame_results.append(
            frame_metrics(candidate, truth, list(case["screenColorRgb"]), core)
        )
        alpha_errors.append(
            candidate[:, :, 3].astype(np.float32) / 255.0
            - truth[:, :, 3].astype(np.float32) / 255.0
        )
    if not frame_results:
        raise RuntimeError(f"Quality case has no frames: {case.get('id')}")
    result = {
        key: float(np.mean([item[key] for item in frame_results]))
        for key in frame_results[0]
    }
    result["temporalFlicker"] = (
        float(
            np.mean(
                [
                    np.mean(np.abs(current - previous))
                    for previous, current in zip(
                        alpha_errors, alpha_errors[1:], strict=False
                    )
                ]
            )
        )
        if len(alpha_errors) > 1
        else 0.0
    )
    return result


def median_metrics(cases: list[dict[str, float]]) -> dict[str, float]:
    return {
        key: float(np.median([case[key] for case in cases]))
        for key in cases[0]
    }


def gate(high: dict[str, float], ultra: dict[str, float]) -> dict[str, Any]:
    improvements = {
        key: (
            (high[key] - ultra[key]) / high[key]
            if high[key] > 1e-12
            else (0.0 if ultra[key] <= 1e-12 else -1.0)
        )
        for key in MAJOR_METRICS
    }
    ordered_alpha_gains = sorted(
        (improvements[key] for key in ALPHA_METRICS), reverse=True
    )
    second_best_alpha_gain = ordered_alpha_gains[1]
    hard_case_gain = min(second_best_alpha_gain, improvements["temporalFlicker"])
    regressions = {
        key: value for key, value in improvements.items() if value < -0.02
    }
    max_regression = max(
        (max(0.0, -value) for value in improvements.values()), default=0.0
    )
    passed = (
        hard_case_gain >= 0.10
        and not regressions
        and ultra["subjectDamage"] <= 0.001
    )
    return {
        "passed": passed,
        "contract": "sam3-vs-sam2matting-bplus-v1",
        "hardCaseQualityGainRatio": hard_case_gain,
        "secondBestAlphaMetricGainRatio": second_best_alpha_gain,
        "maxCriticalRegressionRatio": max_regression,
        "improvementRatios": improvements,
        "regressionsOver2Percent": regressions,
        "subjectDamageLimit": 0.001,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate High/Ultra RGBA sequences against ground truth."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("schemaVersion") or 0) != 1:
        raise SystemExit("Unsupported quality manifest schema.")
    cases = list(manifest.get("cases") or [])
    if not cases:
        raise SystemExit("Quality manifest contains no cases.")
    models = manifest.get("models")
    if not isinstance(models, dict):
        raise SystemExit("Quality manifest must bind High and SAM3 model identities.")
    for name in ("high", "ultra"):
        record = models.get(name)
        if not isinstance(record, dict) or not str(record.get("id") or ""):
            raise SystemExit(f"Quality manifest misses models.{name}.id.")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", record["sha256"]
        ):
            raise SystemExit(f"Quality manifest misses models.{name}.sha256.")
    if str(models["ultra"]["id"]) != "sam3":
        raise SystemExit("Quality manifest Ultra model must be sam3.")
    categories = {
        str(category)
        for case in cases
        for category in (case.get("categories") or [])
    }
    missing_categories = sorted(REQUIRED_CATEGORIES - categories)
    if missing_categories:
        raise SystemExit(
            "Quality manifest misses required categories: "
            + ", ".join(missing_categories)
        )
    frame_count = sum(len(case.get("frames") or []) for case in cases)
    if not 200 <= frame_count <= 300:
        raise SystemExit("Formal SAM3 selection requires 200..300 authorized frames.")
    root = manifest_path.parent
    high_cases = [evaluate_case(root, case, "high") for case in cases]
    ultra_cases = [evaluate_case(root, case, "ultra") for case in cases]
    high = median_metrics(high_cases)
    ultra = median_metrics(ultra_cases)
    selection = gate(high, ultra)
    report = {
        "schemaVersion": 1,
        "reportType": "rotoweave-sam3-selection-v1",
        "datasetManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "models": models,
        "caseCount": len(cases),
        "frameCount": frame_count,
        "categories": sorted(categories),
        "highMedian": high,
        "ultraMedian": ultra,
        "selectionGate": {
            "contract": selection["contract"],
            "passed": selection["passed"],
            "hardCaseQualityGainRatio": selection["hardCaseQualityGainRatio"],
            "maxCriticalRegressionRatio": selection["maxCriticalRegressionRatio"],
        },
        "metrics": selection,
    }
    if not all(
        math.isfinite(float(value))
        for group in (high, ultra)
        for value in group.values()
    ):
        raise SystemExit("Quality report contains a non-finite metric.")
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if args.output:
        args.output.resolve().write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if not report["selectionGate"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
