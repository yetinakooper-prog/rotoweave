from __future__ import annotations

import json
import os
import pickle
import subprocess
from pathlib import Path

import pytest

from server.model_center import ModelCenter
from server.repository import RemoteQueueRepository
from contracts.integrity import canonical_sha256
from contracts.model_compatibility import MODEL_COMPATIBILITY_POLICY_DIGEST, profile_configuration_digest
from contracts.model_recipe import ASSET_BY_ROLE, MODEL_RECIPE_ID, PROFILE_ROLES, RECIPE_DIGEST
from worker.cuda_matting.model_runtime import FrozenModelLayout


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = PROJECT_ROOT / "RotoWeaveServer"
CONTRACTS_ROOT = PROJECT_ROOT / "RotoWeaveContracts"
HIGH_PYTHON = SERVER_ROOT / "server-runtimes" / "high" / "runtime" / "python.exe"


def test_slot_candidate_import_enforces_root_path_and_container(tmp_path: Path) -> None:
    repository = RemoteQueueRepository(tmp_path / "queue.sqlite3")
    library = tmp_path / "models"
    library.mkdir()
    candidate = library / "local-vitmatte.pth"
    candidate.write_bytes(b"candidate")
    wrong = library / "model.onnx"
    wrong.write_bytes(b"onnx")
    center = ModelCenter(repository, tmp_path)
    root = center.add_root(str(library), "models")

    imported = center.register_candidate("roi_refine", root["id"], candidate.name)
    assert imported["state"] == "candidate"
    with pytest.raises(ValueError, match="相对路径"):
        center.register_candidate("roi_refine", root["id"], str(candidate))
    with pytest.raises(ValueError, match="相对路径"):
        center.register_candidate("roi_refine", root["id"], "../escape.pth")
    with pytest.raises(ValueError, match="容器类型"):
        center.register_candidate("roi_refine", root["id"], wrong.name)

    link = library / "linked.pth"
    try:
        link.symlink_to(candidate)
    except OSError:
        return
    with pytest.raises(ValueError, match="符号链接|重解析点"):
        center.register_candidate("roi_refine", root["id"], link.name)


def test_schema2_worker_rechecks_structural_receipts_and_actual_file_hash(tmp_path: Path) -> None:
    sources = {}
    for name in ("sam2", "corridor", "vitmatte"):
        source = tmp_path / "sources" / name
        source.mkdir(parents=True)
        sources[name] = str(source)
    assets = {}
    digest_assets = {}
    paths = {}
    for role in PROFILE_ROLES["high"]:
        recipe = ASSET_BY_ROLE[role]
        path = tmp_path / recipe.filename
        path.write_bytes(f"local-compatible-{role}".encode("utf-8"))
        sha = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        receipt = {
            "state": "passed",
            "role": role,
            "bytes": path.stat().st_size,
            "sha256": sha,
            "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
            "observation": {"tensorCount": 1},
            "observationDigest": f"observation-{role}",
        }
        receipt_digest = canonical_sha256(receipt)
        assets[role] = {
            "assetId": f"asset-{role}",
            "modelId": recipe.model_id,
            "role": role,
            "bytes": path.stat().st_size,
            "sha256": sha,
            "revision": recipe.revision,
            "path": str(path),
            "verificationKind": "structural",
            "verificationContractDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
            "verificationReceiptDigest": receipt_digest,
            "verificationReceipt": receipt,
        }
        digest_assets[role] = {
            "id": f"asset-{role}",
            "bytes": path.stat().st_size,
            "sha256": sha,
            "verification_kind": "structural",
            "verification_receipt_digest": receipt_digest,
        }
        paths[role] = path
    profile_digest = profile_configuration_digest("high", digest_assets)
    configuration = tmp_path / "configuration.json"
    configuration.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "configurationDigest": profile_digest,
                "profileConfigurationDigest": profile_digest,
                "recipeId": MODEL_RECIPE_ID,
                "recipeDigest": RECIPE_DIGEST,
                "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
                "profile": "high",
                "runtime": {"adapter": "worker.cuda_matting.rotoweave_adapter"},
                "assets": assets,
                "sources": sources,
            }
        ),
        encoding="utf-8",
    )
    layout = FrozenModelLayout.from_configuration(configuration)
    assert layout.vitmatte_checkpoint == paths["roi_refine"]
    paths["roi_refine"].write_bytes(b"replaced-after-verification")
    with pytest.raises(RuntimeError, match="identity is invalid|hash changed"):
        FrozenModelLayout.from_configuration(configuration)


@pytest.mark.skipif(not HIGH_PYTHON.is_file(), reason="fixed High runtime is unavailable")
def test_fixed_inspector_accepts_tensor_checkpoint_and_blocks_malicious_pickle(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join((str(SERVER_ROOT), str(CONTRACTS_ROOT))),
    }
    safe = tmp_path / "different-sha.pth"
    subprocess.run(
        [
            str(HIGH_PYTHON),
            "-c",
            "import torch,sys; torch.save({'state_dict': {'weight': torch.ones(2,3)}, 'metadata': {'revision': 2}}, sys.argv[1])",
            str(safe),
        ],
        check=True,
        cwd=SERVER_ROOT,
        env=environment,
    )
    accepted = subprocess.run(
        [str(HIGH_PYTHON), "-m", "worker.cuda_matting.checkpoint_inspector", "--role", "roi_refine", "--path", str(safe)],
        check=False,
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    accepted_payload = json.loads(accepted.stdout.strip().splitlines()[-1])
    assert accepted.returncode == 0
    assert accepted_payload["state"] == "passed"
    assert accepted_payload["observation"]["tensorCount"] == 1

    marker = tmp_path / "payload-executed.txt"

    class Malicious:
        def __reduce__(self):
            expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
            return eval, (expression,)

    malicious = tmp_path / "malicious.pth"
    with malicious.open("wb") as stream:
        pickle.dump(Malicious(), stream)
    rejected = subprocess.run(
        [str(HIGH_PYTHON), "-m", "worker.cuda_matting.checkpoint_inspector", "--role", "roi_refine", "--path", str(malicious)],
        check=False,
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rejected_payload = json.loads(rejected.stdout.strip().splitlines()[-1])
    assert rejected.returncode != 0
    assert rejected_payload["state"] == "failed"
    assert not marker.exists()
