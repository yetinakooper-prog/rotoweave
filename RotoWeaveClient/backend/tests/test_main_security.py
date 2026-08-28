from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from backend.app.config import Settings
from backend.app.api import service as service_routes
from backend.app.main import _is_expected_windows_client_disconnect, create_app
from backend.app.api.presenters import _public_job
from backend.app.remote_protocol import RemoteServiceStatus
from backend.app.workspace_format import atomic_write_json, finalize_aggregate


def test_public_presenters_recursively_hide_workspace_and_runtime_paths() -> None:
    job = _public_job(
        {
            "id": "job_demo",
            "request": {"import_path": r"E:\\Runtime\\incoming.png"},
            "result": {
                "mask_directory": r"E:\\Workspace\\masks",
                "screen_models": {"frm_demo": r"E:\\Runtime\\model.npy"},
                "nested": {"value": r"E:\\Runtime\\private.bin", "ok": True},
                "assetSha256ByFrame": {
                    "frm_demo": {
                        "matte_path": "a" * 64,
                        "delivery_base_path": "b" * 64,
                    }
                },
            },
        }
    )
    assert "request" not in job
    assert "mask_directory" not in job["result"]
    assert "screen_models" not in job["result"]
    assert job["result"]["nested"] == {"value": None, "ok": True}
    assert job["result"]["assetSha256ByFrame"] == {
        "frm_demo": {"matte": "a" * 64, "delivery_base": "b" * 64}
    }


def test_expected_windows_sse_disconnect_filter_is_narrow() -> None:
    reset = ConnectionResetError(10054, "remote host closed the connection")
    reset.winerror = 10054
    expected = {
        "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
        "exception": reset,
    }
    assert _is_expected_windows_client_disconnect(expected) is True
    assert _is_expected_windows_client_disconnect({
        **expected,
        "message": "application background task failed",
    }) is False
    assert _is_expected_windows_client_disconnect({
        "message": expected["message"],
        "exception": RuntimeError("real bug"),
    }) is False


def _configured_app(tmp_path: Path, *, require_token: bool = True):
    settings = Settings(
        data_root=tmp_path / "data",
        runtime_root=tmp_path,
        session_token="current-session-token-123456",
        require_session_token=require_token,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Test Workspace")
    return settings, app


@pytest.mark.anyio
async def test_bootstrap_cookie_is_http_only_one_time_and_query_tokens_are_rejected(
    tmp_path: Path,
) -> None:
    settings, app = _configured_app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        health = await client.get("/api/v4/health")
        assert health.status_code == 200
        assert "token" not in health.json()
        assert "session" not in health.json()

        protected = await client.get("/api/v4/domain")
        assert protected.status_code == 401
        api_docs = await client.get("/api/v4/docs")
        assert api_docs.status_code == 401
        assert (await client.get("/docs")).status_code == 404

        bootstrap = settings.create_bootstrap_token()
        accepted = await client.post(
            "/api/v4/session/bootstrap", json={"token": bootstrap}
        )
        assert accepted.status_code == 204
        cookie = accepted.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "path=/" in cookie
        assert "max-age=31536000" in cookie
        characters = await client.get("/api/v4/domain")
        assert characters.status_code == 200
        assert characters.json()["characters"] == []

        replay = await client.post(
            "/api/v4/session/bootstrap", json={"token": bootstrap}
        )
        assert replay.status_code == 401

    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as anonymous:
        query_token = await anonymous.get(
            f"/api/v4/jobs/events?token={settings.session_token}"
        )
        assert query_token.status_code == 401
        header_session = await anonymous.get(
            "/api/v4/domain",
            headers={"X-RotoWeave-Session": settings.session_token},
        )
        assert header_session.status_code == 200
        legacy_header_session = await anonymous.get(
            "/api/v4/domain",
            headers={"X-AIFrame-Session": settings.session_token},
        )
        assert legacy_header_session.status_code == 200
        conflicting_header_session = await anonymous.get(
            "/api/v4/domain",
            headers={
                "X-RotoWeave-Session": settings.session_token,
                "X-AIFrame-Session": "different",
            },
        )
        assert conflicting_header_session.status_code == 400
        assert conflicting_header_session.json()["code"] == "identity_conflict"
        header_docs = await anonymous.get(
            "/api/v4/docs",
            headers={"X-RotoWeave-Session": settings.session_token},
        )
        assert header_docs.status_code == 200


@pytest.mark.anyio
async def test_v4_local_service_rejects_every_remote_workspace_route(
    tmp_path: Path,
) -> None:
    settings, app = _configured_app(tmp_path)
    remote_transport = httpx.ASGITransport(
        app=app, client=("192.168.1.50", 43110)
    )
    async with httpx.AsyncClient(
        transport=remote_transport, base_url="http://192.168.1.20"
    ) as remote:
        requests = [
            await remote.get("/api/v4/health"),
            await remote.post("/api/v4/session/client"),
            await remote.get("/api/v4/workspace"),
            await remote.post(
                "/api/v4/workspace/client/create",
                json={"name": "Remote Workspace"},
            ),
            await remote.post(
                "/api/v4/domain",
                json={"name": "Remote Character"},
            ),
        ]
        assert {response.status_code for response in requests} == {403}
        assert {
            response.json().get("code") for response in requests
        } == {"local_service_only"}


@pytest.mark.anyio
async def test_local_remote_service_settings_and_no_auth_connection_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "client-state" / "client-launcher.json"
    monkeypatch.setenv("ROTOWEAVE_CLIENT_LAUNCHER_CONFIG", str(config_path))
    _, app = _configured_app(tmp_path, require_token=False)
    transport = httpx.ASGITransport(app=app)

    class FakeRemoteClient:
        def __init__(self, config):
            assert config.service_url == "http://192.168.1.40:8443"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def probe(self):
            return RemoteServiceStatus(
                protocolVersion=1,
                service="RotoWeave Remote Matting 4.0",
                ready=True,
                startupState="ready",
                workerState="ready",
                ownership="short-lived-remote-jobs-only",
            )

    monkeypatch.setattr(service_routes, "RemoteMattingClient", FakeRemoteClient)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        initial = await client.get("/api/v4/remote-service/settings")
        assert initial.status_code == 200
        assert initial.json()["enabled"] is False
        saved = await client.put(
            "/api/v4/remote-service/settings",
            data={"enabled": "true", "host": "192.168.1.40", "port": "8443"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json() == {
            "enabled": True,
            "endpoint": "http://192.168.1.40:8443",
            "host": "192.168.1.40",
            "port": 8443,
        }
        tested = await client.post("/api/v4/remote-service/test")
        assert tested.status_code == 200, tested.text
        assert tested.json()["connected"] is True
        assert tested.json()["ready"] is True
        assert "queue" not in tested.json()
        assert "models" not in tested.json()

        class UnreachableRemoteClient(FakeRemoteClient):
            async def probe(self):
                raise httpx.ConnectError("synthetic connection failure with private details")

        monkeypatch.setattr(service_routes, "RemoteMattingClient", UnreachableRemoteClient)
        failed = await client.post("/api/v4/remote-service/test")
        assert failed.status_code == 502
        assert failed.json()["detail"]["code"] == "remote_service_unreachable"
        assert "private details" not in failed.text

    config_text = config_path.read_text(encoding="utf-8")
    assert "bearer" not in config_text.casefold()
    assert "certificate" not in config_text.casefold()
    assert "workspace" not in config_text.casefold()


@pytest.mark.anyio
async def test_retired_api_routes_are_absent(tmp_path: Path) -> None:
    settings, app = _configured_app(tmp_path, require_token=False)
    transport = httpx.ASGITransport(app=app)
    retired = [
        ("GET", "/api/v1/health"),
        ("GET", "/api/v4/animations/missing/anchor-layout"),
        ("PUT", "/api/v4/frames/missing/alignment-offset"),
        ("PUT", "/api/v4/animations/missing/alignment-translate"),
        ("PUT", "/api/v4/characters/missing/global-anchor"),
    ]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        for method, path in retired:
            response = await client.request(method, path, json={})
            assert response.status_code == 404, (method, path, response.text)


def test_importing_main_has_no_filesystem_or_worker_side_effects(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "import-only"
    environment = {
        **os.environ,
        "ROTOWEAVE_DATA_ROOT": str(data_root),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "RotoWeaveContracts"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import threading; import backend.app.main; "
                "assert not any(t.name.startswith('rotoweave-jobs-') "
                "for t in threading.enumerate())"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not data_root.exists()


@pytest.mark.anyio
async def test_api_root_reports_actual_docs_path_without_frontend(
    tmp_path: Path,
) -> None:
    _, app = _configured_app(tmp_path, require_token=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.json()["docs"] == "/api/v4/docs"


@pytest.mark.anyio
async def test_stale_size_profile_revision_is_rejected_without_making_workspace_read_only(
    tmp_path: Path,
) -> None:
    _, app = _configured_app(tmp_path, require_token=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        initial = await client.get("/api/v4/size-system")
        revision_id = initial.json()["revisionId"]
        created = await client.post(
            "/api/v4/size-profiles",
            headers={"X-RotoWeave-Revision-Id": revision_id},
            json={"name": "Medium", "width_world": 1.2, "height_world": 1.8},
        )
        assert created.status_code == 201

        stale = await client.post(
            "/api/v4/size-profiles",
            headers={"X-RotoWeave-Revision-Id": revision_id},
            json={"name": "Large", "width_world": 2.4, "height_world": 3.6},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "revision_conflict"
        workspace = await client.get("/api/v4/workspace")
        assert workspace.json()["readOnly"] is False
