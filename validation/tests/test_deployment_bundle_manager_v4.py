from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "RotoWeaveContracts"))

from contracts.deployment_bundles import DeploymentBundleManager


def test_plan_uses_utf8_subprocess_and_exposes_page_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = tmp_path / "scripts" / "rotoweave_bootstrap.py"
    bootstrap.parent.mkdir()
    bootstrap.write_text("# test", encoding="utf-8")
    captured = {}

    class Completed:
        returncode = 0
        stdout = json.dumps({"role": "client", "ready": True})
        stderr = ""

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr("contracts.deployment_bundles.subprocess.run", fake_run)
    result = DeploymentBundleManager(tmp_path, "client").plan()
    assert result["pageExportEnabled"] is True
    assert result["singleActiveExport"] is True
    assert captured["encoding"] == "utf-8"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["env"]["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"
    assert captured["env"]["NPM_CONFIG_AUDIT"] == "false"
    assert captured["env"]["NPM_CONFIG_FUND"] == "false"


def test_error_output_removes_npm_notice_but_preserves_real_error() -> None:
    manager = DeploymentBundleManager(Path.cwd(), "client")
    assert manager._clean_error_output(["npm notice New major version\n", "ERROR: 下载失败\n"]) == "ERROR: 下载失败"
    assert manager._clean_error_output(["npm notice update npm notice ERROR: 精确错误\n"]) == "ERROR: 精确错误"


def test_export_drains_large_stderr_without_deadlock_and_reports_clean_error(tmp_path: Path) -> None:
    bootstrap = tmp_path / "scripts" / "rotoweave_bootstrap.py"
    bootstrap.parent.mkdir()
    bootstrap.write_text(
        "import json, sys\n"
        "sys.stderr.write('npm notice filler\\n' * 10000)\n"
        "sys.stderr.write('ERROR: exact failure\\n')\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'type':'progress','stage':'test','progress':0.5,'message':'test'}), flush=True)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    output.mkdir()
    manager = DeploymentBundleManager(tmp_path, "client")
    token = "c" * 32
    manager._tokens[token] = (output, time.time() + 60)
    started = manager.start(token)

    deadline = time.time() + 10
    current = manager.get(started["id"])
    while current["state"] not in {"completed", "failed", "cancelled"} and time.time() < deadline:
        time.sleep(0.02)
        current = manager.get(started["id"])

    assert current["state"] == "failed"
    assert current["error"] == "ERROR: exact failure"


def test_selection_token_is_one_time_and_only_one_export_can_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = tmp_path / "scripts" / "rotoweave_bootstrap.py"
    bootstrap.parent.mkdir()
    bootstrap.write_text("# test", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    manager = DeploymentBundleManager(tmp_path, "server")
    monkeypatch.setattr(manager, "_run", lambda *_args: None)
    manager._tokens["a" * 32] = (output, time.time() + 60)
    first = manager.start("a" * 32)
    assert first["role"] == "server" and first["state"] == "queued"
    with pytest.raises(ValueError, match="失效"):
        manager.start("a" * 32)
    manager._tokens["b" * 32] = (output, time.time() + 60)
    with pytest.raises(ValueError, match="已有"):
        manager.start("b" * 32)


def test_cancel_and_reveal_do_not_accept_unknown_jobs(tmp_path: Path) -> None:
    manager = DeploymentBundleManager(tmp_path, "client")
    with pytest.raises(KeyError):
        manager.cancel("missing")
    with pytest.raises(KeyError):
        manager.reveal("missing")
