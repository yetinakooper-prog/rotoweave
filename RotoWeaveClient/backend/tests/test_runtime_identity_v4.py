from __future__ import annotations

from pathlib import Path

from backend.app.config import SESSION_COOKIE_NAME, Settings
from contracts.product import (
    APPLICATION_DATA_DIRECTORY,
    DEVELOPMENT_DATA_DIRECTORY,
    RUNTIME_API_PORT,
    RUNTIME_SINGLE_INSTANCE_MUTEX,
    RUNTIME_WEB_DEVELOPMENT_PORT,
)


def test_v4_runtime_identity_is_current() -> None:
    assert APPLICATION_DATA_DIRECTORY == "RotoWeave-4.0"
    assert DEVELOPMENT_DATA_DIRECTORY == "RotoWeave-4.0-Dev"
    assert RUNTIME_SINGLE_INSTANCE_MUTEX == r"Local\RotoWeave.SingleInstance.v4"
    assert SESSION_COOKIE_NAME == "rotoweave_v4_session"
    assert RUNTIME_API_PORT == 8766
    assert RUNTIME_WEB_DEVELOPMENT_PORT == 3000


def test_default_settings_use_v4_data_root_and_port(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("ROTOWEAVE_DATA_ROOT", raising=False)
    monkeypatch.delenv("ROTOWEAVE_PORT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    configured = Settings(runtime_root=tmp_path)

    assert configured.data_root == tmp_path / "RotoWeave-4.0"
    assert configured.local_state_root == configured.data_root


def test_frontend_root_can_be_explicit_for_release_and_browser_audits(
    tmp_path: Path, monkeypatch,
) -> None:
    isolated_frontend = tmp_path / "isolated-frontend"
    monkeypatch.setenv("ROTOWEAVE_FRONTEND_ROOT", str(isolated_frontend))

    configured = Settings(data_root=tmp_path / "data", runtime_root=tmp_path / "runtime")

    assert configured.frontend_candidates[0] == isolated_frontend.resolve()
    assert configured.port == 8766


def test_runtime_environment_overrides_remain_available(
    tmp_path: Path, monkeypatch,
) -> None:
    custom_root = tmp_path / "explicit-runtime"
    monkeypatch.setenv("ROTOWEAVE_DATA_ROOT", str(custom_root))
    monkeypatch.setenv("ROTOWEAVE_PORT", "18999")

    configured = Settings(runtime_root=tmp_path)

    assert configured.data_root == custom_root.resolve()
    assert configured.port == 18999
