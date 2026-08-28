from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SERVER_ROOT = WORKSPACE / "RotoWeaveServer"
CONTRACTS_ROOT = WORKSPACE / "RotoWeaveContracts"
sys.path[:0] = [str(SERVER_ROOT), str(CONTRACTS_ROOT)]

from contracts.integrity import atomic_write_json, sha256_file
from contracts.model_recipe import ASSET_BY_ROLE
from contracts.model_runtime_recipe import runtime_recipe
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


def _repackage_vitmatte(source: Path, target: Path) -> None:
    python = SERVER_ROOT / "server-runtimes" / "high" / str(
        runtime_recipe("high")["pythonRelativePath"]
    )
    if not python.is_file():
        raise RuntimeError("Fixed High runtime is unavailable.")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(python),
            "-c",
            (
                "import sys,torch; "
                "value=torch.load(sys.argv[1],map_location='cpu',weights_only=True,mmap=True); "
                "torch.save(value,sys.argv[2],_use_new_zipfile_serialization=True)"
            ),
            str(source),
            str(target),
        ],
        check=True,
        cwd=SERVER_ROOT,
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join((str(SERVER_ROOT), str(CONTRACTS_ROOT))),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-root", type=Path, default=WORKSPACE / "RotoWeaveModels")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=172800.0)
    args = parser.parse_args()
    models_root = args.models_root.resolve(strict=True)
    data_root = args.data_root.resolve(strict=False)
    temp_root = (WORKSPACE / "Temp").resolve(strict=True)
    try:
        data_root.relative_to(temp_root)
    except ValueError as exc:
        raise RuntimeError("Validation data root must remain under workspace Temp.") from exc
    data_root.mkdir(parents=True, exist_ok=True)
    source = models_root / "library" / ASSET_BY_ROLE["roi_refine"].filename
    repackaged = data_root / "candidate-library" / "ViTMatte_B_Com.local-compatible.pth"
    _repackage_vitmatte(source, repackaged)
    source_sha = sha256_file(source)
    candidate_sha = sha256_file(repackaged)
    if candidate_sha == source_sha:
        raise RuntimeError("Repackaged ViTMatte unexpectedly retained the official SHA.")

    os.environ["ROTOWEAVE_MODELS_ROOT"] = str(models_root)
    repository = RemoteQueueRepository(data_root / "queue.sqlite3")
    center = ModelCenter(repository, SERVER_ROOT)
    processor = CudaMattingRemoteProcessor(data_root, SERVER_ROOT)
    center.set_asset_inspector(processor.inspect_model_candidate)
    center.set_profile_tester(processor.self_test_profile)
    try:
        if center.default_root_id is None:
            raise RuntimeError("RotoWeaveModels/library is unavailable.")
        scan = _wait(center, center.scan([center.default_root_id])["id"], args.timeout)
        official_verify = _wait(center, center.verify_draft()["id"], args.timeout)
        candidate_root = center.add_root(str(repackaged.parent), "isolated-local-compatible")
        candidate = center.register_candidate(
            "roi_refine", candidate_root["id"], repackaged.name
        )
        center.bind("roi_refine", candidate["id"])
        structural_verify = _wait(center, center.verify_draft()["id"], args.timeout)
        self_test = _wait(center, center.self_test()["id"], args.timeout)

        def activate_configuration(payload: dict[str, object]) -> dict[str, object]:
            receipts = payload.get("profileExecutionReceipts") or {}
            resident = next(
                profile for profile in ("high", "ultra") if profile in receipts
            )
            processor.configure_configuration(payload, resident)
            return processor.warmup()

        center.set_activator(activate_configuration)
        activate = _wait(center, center.activate()["id"], args.timeout)
        snapshot = center.snapshot()
        profiles = snapshot["profiles"]
        if any(profiles[profile]["state"] != "ready" for profile in ("high", "ultra")):
            raise RuntimeError("High and Ultra did not both reach READY.")
        if any(profiles[profile]["qualification"] != "local-compatible" for profile in ("high", "ultra")):
            raise RuntimeError("Repackaged shared ViTMatte did not produce local-compatible qualification.")
        active = snapshot.get("activeConfiguration") or {}
        active_asset = dict((active.get("assets") or {}).get("roi_refine") or {})
        active_asset.pop("verificationReceipt", None)
        if active_asset.get("sha256") != candidate_sha or active_asset.get("verificationKind") != "structural":
            raise RuntimeError("Active provenance did not retain the structural candidate identity.")
        result = {
            "schemaVersion": 1,
            "modelsRoot": str(models_root),
            "dataRoot": str(data_root),
            "sourceViTMatte": {"path": str(source), "sha256": source_sha},
            "repackagedViTMatte": {"path": str(repackaged), "sha256": candidate_sha},
            "scan": scan,
            "officialVerify": official_verify,
            "structuralVerify": structural_verify,
            "selfTest": self_test,
            "activate": activate,
            "profiles": profiles,
            "activeConfigurationDigest": active.get("configurationDigest"),
            "activeRoiRefine": active_asset,
        }
        if args.output:
            output = args.output.resolve(strict=False)
            try:
                output.relative_to(temp_root)
            except ValueError as exc:
                raise RuntimeError("Validation report must remain under workspace Temp.") from exc
            atomic_write_json(output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        processor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
