from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

SERVER_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = SERVER_ROOT.parent / "RotoWeaveContracts"
for import_root in (SERVER_ROOT, CONTRACTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from server.config import RemoteServerSettings
from server.service import RemoteService


TERMINAL = {"passed", "failed", "cancelled"}
NO_READY_PROFILE_ERROR = "没有任何 Profile 完成四模式自检；各档位结果已保留。"


class ModelIntegrityError(RuntimeError):
    pass


def wait_operation(
    service: RemoteService,
    operation: dict[str, Any],
    timeout: float = 7200.0,
    *,
    allow_failed: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    operation_id = str(operation["id"])
    last: tuple[str, int] | None = None
    while time.monotonic() < deadline:
        current = service.model_center.operation(operation_id)
        state = str(current["state"])
        progress = int(float(current.get("progress") or 0) * 100)
        marker = (str(current.get("stage") or state), progress)
        if marker != last:
            print(json.dumps({"operationId": operation_id, "state": state, "stage": marker[0], "progress": progress}, ensure_ascii=False), flush=True)
            last = marker
        if state in TERMINAL:
            if state != "passed" and not (allow_failed and state == "failed"):
                raise RuntimeError(str(current.get("error") or f"{operation_id} ended as {state}"))
            return current
        time.sleep(0.5)
    service.model_center.cancel_operation(operation_id)
    raise TimeoutError(f"Model operation timed out: {operation_id}")


def run_step(service: RemoteService, name: str, start: Callable[[], dict[str, Any]]) -> None:
    print(f"[model-setup] {name}", flush=True)
    wait_operation(service, start())


def run_profile_self_test(service: RemoteService) -> tuple[dict[str, Any], list[str]]:
    """Accept only the model center's explicit all-Profiles-unavailable result."""

    center = service.model_center
    print("[model-setup] self-test-high-ultra", flush=True)
    operation = wait_operation(service, center.self_test(), allow_failed=True)
    tested = center.snapshot()
    ready_profiles = [
        name for name, value in tested["profiles"].items()
        if value.get("state") == "ready"
    ]
    if operation["state"] == "failed":
        error = str(operation.get("error") or "")
        if ready_profiles or error != NO_READY_PROFILE_ERROR:
            raise RuntimeError(error or "Profile self-test failed unexpectedly.")
    elif not ready_profiles:
        raise RuntimeError("Profile self-test passed without a READY Profile.")
    return tested, ready_profiles


def main() -> int:
    project_root = SERVER_ROOT.parent
    os.environ.setdefault("ROTOWEAVE_MODELS_ROOT", str(project_root / "RotoWeaveModels"))
    settings = RemoteServerSettings()
    service = RemoteService(settings)
    service.start()
    try:
        center = service.model_center
        if not center.default_root_id:
            raise RuntimeError("Default RotoWeaveModels/library root is unavailable.")
        run_step(service, "select-default-models", center.select_default)
        snapshot = center.snapshot()
        missing = [slot["role"] for slot in snapshot["slots"] if not slot.get("binding")]
        if missing:
            raise ModelIntegrityError("Five-model binding is incomplete: " + ", ".join(missing))
        try:
            run_step(service, "verify-draft", center.verify_draft)
        except RuntimeError as exc:
            raise ModelIntegrityError(str(exc)) from exc
        tested, ready_profiles = run_profile_self_test(service)
        if not ready_profiles:
            print(json.dumps({
                "ready": False,
                "installed": True,
                "profiles": tested["profiles"],
                "warning": "No Profile passed self-test; installation remains usable.",
            }, ensure_ascii=False), flush=True)
            return 0
        run_step(service, "activate", center.activate)
        final = center.snapshot()
        if not final.get("activeConfiguration") or not any(item["state"] == "ready" for item in final["profiles"].values()):
            raise RuntimeError("Model configuration did not reach a partially active ready state.")
        print(json.dumps({"ready": True, "activeConfiguration": final["activeConfiguration"], "profiles": final["profiles"]}, ensure_ascii=False), flush=True)
        return 0
    finally:
        service.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelIntegrityError as exc:
        print(f"[model-integrity-error] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(3)
