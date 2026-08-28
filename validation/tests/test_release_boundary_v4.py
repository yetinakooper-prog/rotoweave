from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

from contracts.integrity import canonical_sha256
from contracts.model_runtime_recipe import runtime_recipe


WORKSPACE = Path(__file__).resolve().parents[2]


def test_release_sources_enforce_client_independent_model_separation() -> None:
    completed = subprocess.run(
        [sys.executable, str(WORKSPACE / "validation" / "scripts" / "validate-release-boundary.py")],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["passed"] is True
    assert report["sources"]["productVersion"] == "4.0.0"
    assert report["sources"]["integratedModelArtifactsIncluded"] is False
    assert report["sources"]["serverIndependentModelsBundledWithClient"] is False
    assert report["sources"]["applicationModelFiles"] == []
    assert report["sources"]["basicArtifactPolicy"] == "build-from-pinned-source"


def test_source_integrity_gate_rejects_retired_worker_roots() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "validation" / "scripts" / "audit-source-integrity.py"),
            "--gate",
        ],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["retiredSourceRoots"] == []
    assert report["summary"]["retiredSourceRoots"] == 0
    namespace = runpy.run_path(
        str(WORKSPACE / "validation" / "scripts" / "audit-source-integrity.py")
    )
    simulated = dict(report, retiredSourceRoots=["RotoWeaveServer/worker/matting4090"])
    assert namespace["_gate_failed"](simulated) is True


def test_release_validator_accepts_pyinstaller_6_internal_runtime_layout(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "RotoWeave"
    runtime_root = dist_root / "_internal"
    runtime_root.mkdir(parents=True)

    completed = subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "validation" / "scripts" / "validate-release-boundary.py"),
            "--dist-root",
            str(dist_root),
        ],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    compiled = report["compiledApplication"]
    assert compiled["pyInstallerLayout"] == "onedir-internal"
    assert compiled["applicationModelFiles"] == []
    assert compiled["basicArtifactPolicy"] == "build-from-pinned-source"
    assert Path(compiled["runtimeRoot"]) == runtime_root.resolve()


def test_release_validator_rejects_embedded_generated_basic(tmp_path: Path) -> None:
    dist_root = tmp_path / "RotoWeave"
    models = dist_root / "_internal" / "models"
    models.mkdir(parents=True)
    (models / "birefnet-lite-matting.onnx").write_bytes(b"forbidden")

    completed = subprocess.run(
        [
            sys.executable,
            str(WORKSPACE / "validation" / "scripts" / "validate-release-boundary.py"),
            "--dist-root",
            str(dist_root),
        ],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "must not embed" in completed.stdout


def test_server_release_declares_two_fixed_weightless_runtime_profiles() -> None:
    for profile in ("high", "ultra"):
        runtime = runtime_recipe(profile)
        identity = dict(runtime)
        digest = identity.pop("digest")
        assert digest == canonical_sha256(identity)
        assert runtime["profile"] == profile
        assert runtime["adapter"] == "worker.cuda_matting.rotoweave_adapter"
    spec = (WORKSPACE / "RotoWeaveServer" / "RotoWeaveServer.spec").read_text(encoding="utf-8")
    build = (WORKSPACE / "RotoWeaveServer" / "scripts" / "build-windows-server.ps1").read_text(encoding="utf-8")
    preparer = (WORKSPACE / "RotoWeaveServer" / "scripts" / "prepare-server-runtimes.py").read_text(encoding="utf-8")
    assert "server-runtimes" in spec and "ROTOWEAVE_SERVER_RUNTIMES_STAGE" in spec
    assert "model-pack-public-key.hex" not in spec
    assert "prepare-server-runtimes.py" in build
    assert "Temp\\CudaRuntime" not in build
    assert '{".pt", ".pth", ".safetensors", ".ckpt"}' in preparer


def test_runtime_staging_removes_predecessor_product_worker_payload() -> None:
    source = (
        WORKSPACE / "RotoWeaveServer" / "scripts" / "prepare-server-runtimes.py"
    ).read_text(encoding="utf-8")

    assert "_remove_predecessor_product_runtime" in source
    assert 'runtime_root / ".aiframe-matting4090-ready.json"' in source
    assert '"site-packages" / "aiframe_matting4090_worker"' in source
