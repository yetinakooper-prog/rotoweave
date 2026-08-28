from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.app.config import Settings
from backend.app.main import create_app
from contracts.deployment_bundles import DeploymentBundleManager


@pytest.mark.anyio
async def test_client_deployment_bundle_routes_require_loopback_and_local_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        DeploymentBundleManager,
        "plan",
        lambda self: {"role": self.role, "ready": True, "pageExportEnabled": True},
    )
    settings = Settings(data_root=tmp_path / "data", runtime_root=tmp_path, require_session_token=True)
    app = create_app(settings)
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 45000))
    async with httpx.AsyncClient(transport=local, base_url="http://127.0.0.1:8765") as client:
        path = "/api/v4/deployment-bundles/plan"
        assert (await client.get(path)).status_code == 401
        response = await client.get(path, headers={"X-RotoWeave-Session": settings.session_token})
        assert response.status_code == 200 and response.json()["role"] == "client"
        assert (
            await client.get(
                path,
                headers={"X-RotoWeave-Session": settings.session_token, "Origin": "https://evil.example"},
            )
        ).status_code == 403
    remote = httpx.ASGITransport(app=app, client=("10.1.2.3", 45001))
    async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1:8765") as client:
        assert (
            await client.get(
                "/api/v4/deployment-bundles/plan",
                headers={"X-RotoWeave-Session": settings.session_token},
            )
        ).status_code == 403
