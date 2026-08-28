from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.workspace_session import WorkspaceSessionManager
from backend.app.workspace_format import atomic_write_json
from backend.app.workspace_repository import WorkspaceRepository


def _open(tmp_path: Path):
    session = WorkspaceSessionManager(Settings(data_root=tmp_path / "runtime", runtime_root=tmp_path))
    session.create(tmp_path / "workspace", "Retired Undo Workspace")
    return session, session.require_repository(), tmp_path / "workspace"


def _revision(repository: WorkspaceRepository) -> str:
    return str(repository.workspace_domain()["revisionId"])


def _asset(root: Path, logical: str, payload: bytes) -> str:
    target = root / logical
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return logical


def test_deleted_material_files_are_reclaimed_immediately(tmp_path: Path) -> None:
    _, repository, root = _open(tmp_path)
    character = repository.create_domain_character("角色", expected_revision_id=_revision(repository))
    video = _asset(root, "materials/source/video.mp4", b"video")
    frame = _asset(root, "materials/source/frames/000000.png", b"frame")
    source = repository.create_material_source(
        character["id"], "素材", video, [frame], expected_revision_id=_revision(repository)
    )

    cleanup = repository.delete_material_source(
        source["id"], explicit=True, expected_revision_id=_revision(repository)
    )
    assert repository.remove_domain_asset_files(cleanup["assetPaths"]) == 10
    assert not (root / video).exists()
    assert not (root / frame).exists()


def test_valid_legacy_ledger_deletes_only_currently_unreferenced_paths(tmp_path: Path) -> None:
    _, repository, root = _open(tmp_path)
    character = repository.create_domain_character("角色", expected_revision_id=_revision(repository))
    referenced = _asset(root, "materials/source/video.mp4", b"video")
    frame = _asset(root, "materials/source/frames/000000.png", b"frame")
    repository.create_material_source(
        character["id"], "素材", referenced, [frame], expected_revision_id=_revision(repository)
    )
    orphan = _asset(root, "materials/orphan/model.bin", b"orphan")
    ledger = repository.runtime.path.parent / "workspace-undo-pending.json"
    atomic_write_json(ledger, {
        "schemaVersion": 1,
        "workspaceId": repository.workspace_id,
        "paths": [referenced, orphan],
    })

    WorkspaceRepository(root, repository.runtime)

    assert (root / referenced).is_file()
    assert not (root / orphan).exists()
    assert not ledger.exists()


@pytest.mark.parametrize("payload", [
    {"schemaVersion": 1, "workspaceId": "wrong", "paths": ["materials/orphan.bin"]},
    {"schemaVersion": 1, "workspaceId": "placeholder", "paths": ["../outside.bin"]},
])
def test_invalid_or_foreign_legacy_ledger_only_removes_manifest(tmp_path: Path, payload: dict) -> None:
    _, repository, root = _open(tmp_path)
    candidate = _asset(root, "materials/orphan.bin", b"keep")
    ledger = repository.runtime.path.parent / "workspace-undo-pending.json"
    if payload["workspaceId"] == "placeholder":
        payload["workspaceId"] = repository.workspace_id
    atomic_write_json(ledger, payload)

    WorkspaceRepository(root, repository.runtime)

    assert (root / candidate).read_bytes() == b"keep"
    assert not ledger.exists()


def test_domain_save_finalizes_and_validates_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, repository, _ = _open(tmp_path)
    import backend.app.workspace_repository as module

    calls = {"finalize": 0, "validate": 0}
    original_finalize = module.finalize_aggregate
    original_validate = module.validate_workspace_domain

    def finalize(*args, **kwargs):
        calls["finalize"] += 1
        return original_finalize(*args, **kwargs)

    def validate(*args, **kwargs):
        calls["validate"] += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(module, "finalize_aggregate", finalize)
    monkeypatch.setattr(module, "validate_workspace_domain", validate)
    repository.create_domain_character("角色", expected_revision_id=_revision(repository))
    assert calls == {"finalize": 1, "validate": 1}


@pytest.mark.anyio
async def test_workspace_undo_routes_are_retired(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", runtime_root=tmp_path, require_session_token=False)
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Retired Undo API")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 44001))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/api/v4/workspace/undo")).status_code == 404
        assert (await client.post("/api/v4/workspace/undo", json={})).status_code == 404
        assert (await client.delete("/api/v4/workspace/undo")).status_code == 404
