from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.mattebench import MatteBenchError, aggregate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate paired Hybrid Matte v5 / RotoWeave 3.0 MatteBench "
            "measurements by route and category, with median, p90, p95, "
            "bootstrap confidence intervals, and release-blocking gates."
        )
    )
    parser.add_argument("samples", type=Path, help="MatteBench-v1 JSON measurements.")
    parser.add_argument("--output", type=Path, help="Optional report JSON path.")
    parser.add_argument("--bootstrap-resamples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=4090)
    args = parser.parse_args()
    try:
        payload = json.loads(args.samples.resolve().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise MatteBenchError("MatteBench input must be a JSON object.")
        report = aggregate(
            payload, resamples=args.bootstrap_resamples, seed=args.seed
        )
    except (OSError, json.JSONDecodeError, MatteBenchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        target = args.output.resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
