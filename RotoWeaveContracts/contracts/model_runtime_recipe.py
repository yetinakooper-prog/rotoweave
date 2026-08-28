from __future__ import annotations

from typing import Any

from .integrity import canonical_sha256


_BASE: dict[str, dict[str, Any]] = {
    "high": {
        "schemaVersion": 1,
        "id": "rotoweave-high-runtime-v1",
        "profile": "high",
        "adapter": "worker.cuda_matting.rotoweave_adapter",
        "pythonRelativePath": "runtime/python.exe",
        "pythonVersion": "3.10.11",
        "torch": "2.8.0+cu128",
        "cuda": "12.8",
        "layout": "embedded-python",
        "dependencyIdentity": "b027910d69e01ae76263ebd53fca639e5fe401ecf3e63fd8cfd62bd4b7d2aa4e",
        "requirementsSha256": "b027910d69e01ae76263ebd53fca639e5fe401ecf3e63fd8cfd62bd4b7d2aa4e",
        "runtimeSourceContractSha256": "3bb6ff9a261c79562b9ebfe37647d74fa2f99f4572e707c9095271393c968581",
        "sourceRevisions": {
            "SAM2Matting": "73dd721d77b56749248aefe5e8824d7f61b9d13c",
            "CorridorKey": "97e55a453060745bead1befd293f6e523c4b845c",
            "ViTMatte": "8cd7ef068380977c3962c4cb733cb1fe7f2241a5",
        },
    },
    "ultra": {
        "schemaVersion": 1,
        "id": "rotoweave-ultra-runtime-v1",
        "profile": "ultra",
        "adapter": "worker.cuda_matting.rotoweave_adapter",
        "sam3AdapterContract": "rotoweave-sam3-alpha-v1",
        "pythonRelativePath": "runtime/python.exe",
        "pythonVersion": "3.10.11",
        "torch": "2.8.0+cu128",
        "cuda": "12.8",
        "layout": "embedded-python-overlay",
        "baseProfile": "high",
        "dependencyIdentity": "c39e2655cccc1127580b9815859e1c2d71971cac1170440c2e365be9ffb31983",
        "requirementsSha256": "f45480cdfc09981ab8a5a6f4a968e258a7dfde25bc6d077245013ab277316d24",
        "runtimeSourceContractSha256": "3bb6ff9a261c79562b9ebfe37647d74fa2f99f4572e707c9095271393c968581",
        "sourceRevisions": {
            "SAM2Matting": "73dd721d77b56749248aefe5e8824d7f61b9d13c",
            "CorridorKey": "97e55a453060745bead1befd293f6e523c4b845c",
            "ViTMatte": "8cd7ef068380977c3962c4cb733cb1fe7f2241a5",
            "SAM3": "96914d2425f90a64f45ca977c2b5165418099543",
        },
    },
}


def runtime_recipe(profile: str) -> dict[str, Any]:
    if profile not in _BASE:
        raise KeyError(profile)
    payload = {**_BASE[profile], "sourceRevisions": dict(_BASE[profile]["sourceRevisions"])}
    payload["digest"] = canonical_sha256(payload)
    return payload


RUNTIMES = {profile: runtime_recipe(profile) for profile in ("high", "ultra")}
