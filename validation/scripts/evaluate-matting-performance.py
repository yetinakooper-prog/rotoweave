from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np


def percentile(values: Any, value: float) -> float:
    samples = np.asarray(list(values or []), dtype=np.float64)
    if samples.size < 3 or not np.all(np.isfinite(samples)):
        raise RuntimeError("Each performance series requires at least three finite samples.")
    return float(np.percentile(samples, value))


def evaluate(samples: dict[str, Any]) -> dict[str, Any]:
    cpu_birefnet = percentile(samples.get("cpuBiRefNetMs"), 50)
    cuda_birefnet = percentile(samples.get("cudaBiRefNetMs"), 50)
    baseline_high = percentile(samples.get("baselineHighMs"), 50)
    cuda_high = percentile(samples.get("cudaHighMs"), 50)
    metrics = {
        "cudaBiRefNetSpeedup": cpu_birefnet / max(cuda_birefnet, 1e-9),
        "highEndToEndSpeedup": baseline_high / max(cuda_high, 1e-9),
        "gpuPeakMiB": percentile(samples.get("gpuPeakMiB"), 100),
        "gpuGrowthMiB": percentile(samples.get("gpuEndMiB"), 50)
        - percentile(samples.get("gpuStartMiB"), 50),
        "webgl1080P95Ms": percentile(samples.get("webgl1080Ms"), 95),
        "webgl4kP95Ms": percentile(samples.get("webgl4kMs"), 95),
        "brushRoiComputeP95Ms": percentile(samples.get("brushRoiComputeMs"), 95),
        "commit1080P95Ms": percentile(samples.get("commit1080Ms"), 95),
        "commit4kP95Ms": percentile(samples.get("commit4kMs"), 95),
    }
    checks = {
        "cudaBiRefNetAtLeast3x": metrics["cudaBiRefNetSpeedup"] >= 3.0,
        "highEndToEndAtLeast2x": metrics["highEndToEndSpeedup"] >= 2.0,
        "gpuPeakAtMost5_5GiB": metrics["gpuPeakMiB"] <= 5.5 * 1024,
        "gpuNoGrowth": metrics["gpuGrowthMiB"] <= 32.0,
        "webgl1080P95": metrics["webgl1080P95Ms"] <= 16.7,
        "webgl4kP95": metrics["webgl4kP95Ms"] <= 33.0,
        "brushRoiP95": metrics["brushRoiComputeP95Ms"] <= 100.0,
        "commit1080P95": metrics["commit1080P95Ms"] <= 500.0,
        "commit4kP95": metrics["commit4kP95Ms"] <= 1500.0,
    }
    return {"metrics": metrics, "checks": checks, "passed": all(checks.values())}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply ADR 0024 warm-cache GPU and brush performance gates."
    )
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.samples.resolve().read_text(encoding="utf-8"))
    if int(payload.get("schemaVersion") or 0) != 1:
        raise SystemExit("Unsupported performance sample schema.")
    report = {"schemaVersion": 1, **evaluate(payload)}
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.resolve().write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
