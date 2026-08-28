from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SERVER_ROOT = WORKSPACE / "RotoWeaveServer"
CONTRACTS_ROOT = WORKSPACE / "RotoWeaveContracts"
sys.path[:0] = [str(SERVER_ROOT), str(CONTRACTS_ROOT)]

from server.model_center import ModelCenter
from server.processor import CudaMattingRemoteProcessor
from server.repository import RemoteQueueRepository


def _wait(center: ModelCenter, operation_id: str, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = center.operation(operation_id)
        if operation["state"] not in {"queued", "running"}:
            if operation["state"] != "passed":
                raise RuntimeError(f"{operation['kind']} failed: {operation.get('error')}")
            return operation
        time.sleep(0.2)
    raise TimeoutError(f"model operation timed out: {operation_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-root", type=Path, default=WORKSPACE / "RotoWeaveModels")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--gpu-self-test", action="store_true")
    parser.add_argument("--timeout", type=float, default=7200.0)
    args = parser.parse_args()
    models_root = args.models_root.resolve(strict=True)
    data_root = args.data_root.resolve(strict=False)
    temp_root = (WORKSPACE / "Temp").resolve(strict=True)
    try:
        data_root.relative_to(temp_root)
    except ValueError as exc:
        raise RuntimeError("Validation data root must remain under workspace Temp.") from exc
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["ROTOWEAVE_MODELS_ROOT"] = str(models_root)
    repository = RemoteQueueRepository(data_root / "queue.sqlite3")
    center = ModelCenter(repository, SERVER_ROOT)
    if center.default_root_id is None:
        raise RuntimeError("RotoWeaveModels/library is unavailable.")
    scan = _wait(center, center.scan([center.default_root_id])["id"], args.timeout)
    verify = _wait(center, center.verify_draft()["id"], args.timeout)
    processor = None
    self_test = None
    activate = None
    try:
        if args.gpu_self_test:
            processor = CudaMattingRemoteProcessor(data_root, SERVER_ROOT)
            center.set_profile_tester(processor.self_test_profile)

            def activate_configuration(payload: dict[str, object]) -> dict[str, object]:
                assert processor is not None
                processor.configure_configuration(payload, "high")
                return processor.warmup()

            center.set_activator(activate_configuration)
            self_test = _wait(center, center.self_test()["id"], args.timeout)
            activate = _wait(center, center.activate()["id"], args.timeout)
        snapshot = center.snapshot()
        result = {
            "schemaVersion": 1,
            "modelsRoot": str(models_root),
            "dataRoot": str(data_root),
            "scan": scan,
            "verify": verify,
            "selfTest": self_test,
            "activate": activate,
            "draftConfigurationDigest": snapshot["draftConfigurationDigest"],
            "activeConfigurationDigest": (
                (snapshot.get("activeConfiguration") or {}).get("configurationDigest")
                if isinstance(snapshot.get("activeConfiguration"), dict)
                else None
            ),
            "profiles": snapshot["profiles"],
            "slotStates": {item["role"]: item["state"] for item in snapshot["slots"]},
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if processor is not None:
            processor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
