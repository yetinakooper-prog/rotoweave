from __future__ import annotations

import asyncio
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import httpx
import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from backend.app.config import Settings
from backend.app.api import materials as materials_api
from backend.app import jobs as jobs_module
from backend.app.main import create_app
from backend.app.material_photoshop_sheet_v4 import (
    export_material_sheet,
    import_material_sheet,
    material_sheet_path,
)
from backend.app.workspace_format import WorkspaceFormatError
from backend.app.workspace_session import WorkspaceSessionManager
from backend.app.remote_protocol import RemoteJobState


def _upload(name: str) -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(b"png"))


def test_photoshop_session_filenames_reject_renamed_mixed_missing_and_duplicate_parts() -> None:
    first = "a" * 32
    second = "b" * 32
    resolved, ordered = materials_api._ordered_photoshop_uploads([
        _upload(f"RotoWeave-PS-{first}-part-002-of-002.png"),
        _upload(f"RotoWeave-PS-{first}-part-001-of-002.png"),
    ])
    assert resolved == first
    assert [item.filename for item in ordered] == [
        f"RotoWeave-PS-{first}-part-001-of-002.png",
        f"RotoWeave-PS-{first}-part-002-of-002.png",
    ]
    invalid_sets = [
        [_upload("renamed.png")],
        [
            _upload(f"RotoWeave-PS-{first}-part-001-of-002.png"),
            _upload(f"RotoWeave-PS-{second}-part-002-of-002.png"),
        ],
        [_upload(f"RotoWeave-PS-{first}-part-001-of-002.png")],
        [
            _upload(f"RotoWeave-PS-{first}-part-001-of-002.png"),
            _upload(f"RotoWeave-PS-{first}-part-001-of-002.png"),
        ],
    ]
    for files in invalid_sets:
        with pytest.raises(HTTPException) as caught:
            materials_api._ordered_photoshop_uploads(files)
        assert caught.value.status_code == 422


def _workspace(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "runtime", runtime_root=tmp_path)
    session = WorkspaceSessionManager(settings)
    session.create(tmp_path / "workspace", "Photoshop 4")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"photoshop-source")
    paths: list[str] = []
    metadata: list[dict[str, object]] = []
    for index, size in enumerate(((16, 12), (10, 8))):
        image = Image.new("RGBA", size, (20 + index * 30, 140, 230, 255))
        path = fixture / f"{index:06d}.png"
        image.save(path)
        logical = path.relative_to(repository.root).as_posix()
        paths.append(logical)
        metadata.append({
            "linearPath": logical,
            "ptsUs": index * 41_667,
            "durationUs": 41_667,
            "width": size[0],
            "height": size[1],
        })
    source = repository.create_material_source(
        character["id"],
        "PS Source",
        video.relative_to(repository.root).as_posix(),
        paths,
        metadata={
            "fps": 24.0,
            "durationSeconds": 2 / 24,
            "frameCount": 2,
            "width": 16,
            "height": 12,
            "color": {},
            "warnings": [],
        },
        frame_metadata=metadata,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    return session, repository, source


def test_material_photoshop_round_trip_preserves_stable_mapping_and_creates_variant(tmp_path: Path) -> None:
    session, repository, source = _workspace(tmp_path)
    exported = export_material_sheet(repository, session.runtime_root, source["id"], batch_size=1)
    assert exported["selectedFrameCount"] == 2
    assert exported["batchCount"] == 2
    assert [item["sourceFrameId"] for item in exported["mapping"]] == [
        item["id"] for item in source["frames"]
    ]
    sheet_path = material_sheet_path(session.runtime_root, source["id"], exported["sheetId"])
    with Image.open(sheet_path) as opened:
        edited = opened.convert("RGBA")
    edited.putpixel((0, 0), (255, 0, 0, 128))
    imported = tmp_path / "edited.png"
    edited.save(imported)
    edited.close()

    variant = import_material_sheet(
        repository,
        session.runtime_root,
        source["id"],
        exported["sheetId"],
        [imported, material_sheet_path(session.runtime_root, source["id"], exported["sheetId"], 1)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert variant["kind"] == "photoshop"
    assert variant["settings"]["mappingSha256"] == exported["mappingSha256"]
    assert [item["sourceFrameId"] for item in variant["frames"]] == [
        item["id"] for item in source["frames"]
    ]


def test_material_photoshop_uses_cumulative_partial_variant_baseline(tmp_path: Path) -> None:
    session, repository, source = _workspace(tmp_path)
    first_path = tmp_path / "first-result.png"
    second_path = tmp_path / "second-result.png"
    Image.new("RGBA", (16, 12), (240, 20, 30, 255)).save(first_path)
    Image.new("RGBA", (10, 8), (40, 220, 60, 255)).save(second_path)
    first = repository.publish_material_variant(
        source["id"],
        "basic",
        [str(first_path)],
        {"quality": "basic"},
        source_frame_ids=[source["frames"][0]["id"]],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    second = repository.publish_material_variant(
        source["id"],
        "high",
        [str(second_path)],
        {"quality": "high"},
        source_frame_ids=[source["frames"][1]["id"]],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )

    exported = export_material_sheet(
        repository,
        session.runtime_root,
        source["id"],
        variant_id=second["id"],
        frame_indexes=[0, 1],
        batch_size=1,
    )
    assert exported["sourceFrameCount"] == 2
    assert [item["sourceFrameId"] for item in exported["mapping"]] == [
        item["id"] for item in source["frames"]
    ]
    with Image.open(material_sheet_path(session.runtime_root, source["id"], exported["sheetId"], 0)) as opened:
        assert opened.convert("RGBA").getpixel((0, 0)) == (240, 20, 30, 255)
    with Image.open(material_sheet_path(session.runtime_root, source["id"], exported["sheetId"], 1)) as opened:
        assert opened.convert("RGBA").getpixel((0, 0)) == (40, 220, 60, 255)

    variant = import_material_sheet(
        repository,
        session.runtime_root,
        source["id"],
        exported["sheetId"],
        [
            material_sheet_path(session.runtime_root, source["id"], exported["sheetId"], 0),
            material_sheet_path(session.runtime_root, source["id"], exported["sheetId"], 1),
        ],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert first["id"] != second["id"] != variant["id"]
    assert [item["sourceFrameId"] for item in variant["frames"]] == [
        item["id"] for item in source["frames"]
    ]


def test_material_photoshop_import_rejects_wrong_size_and_tampered_mapping(tmp_path: Path) -> None:
    session, repository, source = _workspace(tmp_path)
    exported = export_material_sheet(repository, session.runtime_root, source["id"])
    wrong = tmp_path / "wrong.png"
    Image.fromarray(np.zeros((3, 3, 4), dtype=np.uint8), mode="RGBA").save(wrong)
    with pytest.raises(WorkspaceFormatError, match="尺寸"):
        import_material_sheet(
            repository,
            session.runtime_root,
            source["id"],
            exported["sheetId"],
            [wrong],
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )

    root = material_sheet_path(session.runtime_root, source["id"], exported["sheetId"]).parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["mapping"][0]["x"] = 1
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkspaceFormatError, match="映射已损坏"):
        import_material_sheet(
            repository,
            session.runtime_root,
            source["id"],
            exported["sheetId"],
            [material_sheet_path(session.runtime_root, source["id"], exported["sheetId"])],
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )


def test_multiple_photoshop_sessions_coexist_and_older_complete_session_remains_importable(
    tmp_path: Path,
) -> None:
    session, repository, source = _workspace(tmp_path)
    first = export_material_sheet(repository, session.runtime_root, source["id"], batch_size=1)
    second = export_material_sheet(repository, session.runtime_root, source["id"], batch_size=2)
    assert first["sheetId"] != second["sheetId"]
    first_files = [
        material_sheet_path(session.runtime_root, source["id"], first["sheetId"], index)
        for index in range(first["batchCount"])
    ]
    assert all(path.is_file() for path in first_files)
    variant = import_material_sheet(
        repository,
        session.runtime_root,
        source["id"],
        first["sheetId"],
        first_files,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert variant["kind"] == "photoshop"


@pytest.mark.anyio
async def test_character_reveal_is_loopback_only_and_uses_canonical_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_root=tmp_path / "reveal-data", runtime_root=tmp_path, require_session_token=False)
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "reveal-workspace", "Reveal")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    opened: list[Path] = []
    monkeypatch.setattr(materials_api.os, "startfile", lambda path: opened.append(Path(path)), raising=False)
    try:
        loopback = httpx.ASGITransport(app=app, client=("127.0.0.1", 45003))
        async with httpx.AsyncClient(transport=loopback, base_url="http://127.0.0.1") as client:
            response = await client.post(f"/api/v4/domain/characters/{character['id']}/reveal")
        assert response.status_code == 200, response.text
        assert opened == [repository.root / "characters" / character["id"]]
        assert opened[0].is_dir()

        remote = httpx.ASGITransport(app=app, client=("192.168.10.8", 45004))
        async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as client:
            forbidden = await client.post(f"/api/v4/domain/characters/{character['id']}/reveal")
        assert forbidden.status_code == 403
    finally:
        app.state.runtime.shutdown()


@pytest.mark.anyio
async def test_multi_file_material_import_job_reports_server_progress_and_can_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_root=tmp_path / "import-data", runtime_root=tmp_path, require_session_token=False)
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "import-workspace", "Import")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    manager = app.state.jobs
    calls: list[str] = []

    def fake_import(
        _library,
        _character_id,
        source_path,
        display_name,
        *,
        target_fps=None,
        expected_revision_id=None,
        report,
        check_control,
    ):
        assert target_fps == 24
        assert expected_revision_id == repository.workspace_domain()["revisionId"]
        check_control()
        report("extracting", 0.5, f"{display_name} 抽帧")
        calls.append(source_path.name)
        return {"source": {"id": f"source-{len(calls)}"}, "duplicate": False, "report": {}}

    monkeypatch.setattr(jobs_module.MaterialLibrary, "import_video", fake_import)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45005))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            created = await client.post(
                f"/api/v4/domain/characters/{character['id']}/materials/import-jobs",
                data={
                    "target_fps": "24",
                    "expected_revision_id": repository.workspace_domain()["revisionId"],
                },
                files=[
                    ("files", ("first.mp4", b"first-video", "video/mp4")),
                    ("files", ("second.mov", b"second-video", "video/quicktime")),
                ],
            )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        manager._run(job_id)
        completed = manager.database.get_job(job_id)
        assert completed["status"] == "completed"
        assert completed["progress"] == 1.0
        assert len(completed["result"]["import"]["imported"]) == 2
        assert len(calls) == 2

        cancellation = manager.create_material_import(
            character["id"],
            (completed["request"] or {})["files"],
            target_fps=24,
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )
        cancelled = manager.cancel(cancellation["id"])
        assert cancelled is not None
        assert cancelled["status"] == "cancelled"
    finally:
        app.state.runtime.shutdown()

@pytest.mark.anyio
async def test_material_workbench_api_exports_imports_and_reports_missing_remote_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ROTOWEAVE_REMOTE_MATTING_URL", raising=False)
    settings = Settings(data_root=tmp_path / "api-data", runtime_root=tmp_path, require_session_token=False)
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "api-workspace", "Material API")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"api-source")
    frame = fixture / "000000.png"
    Image.new("RGBA", (8, 6), (20, 30, 40, 255)).save(frame)
    logical = frame.relative_to(repository.root).as_posix()
    source = repository.create_material_source(
        character["id"], "API Source", video.relative_to(repository.root).as_posix(), [logical],
        metadata={"fps": 24.0, "durationSeconds": 1 / 24, "frameCount": 1, "width": 8, "height": 6, "color": {}, "warnings": []},
        frame_metadata=[{"linearPath": logical, "ptsUs": 0, "durationUs": 41_667, "width": 8, "height": 6}],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45002))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            exported = await client.post(
                f"/api/v4/material-sources/{source['id']}/photoshop-sheet/export", json={}
            )
            assert exported.status_code == 200, exported.text
            manifest = exported.json()
            downloaded = await client.get(manifest["sheets"][0]["downloadUrl"])
            assert downloaded.status_code == 200
            exported_name = (
                f"RotoWeave-PS-{manifest['sheetId']}-part-001-of-"
                f"{len(manifest['sheets']):03d}.png"
            )
            assert exported_name in downloaded.headers["content-disposition"]
            imported = await client.post(
                f"/api/v4/material-sources/{source['id']}/photoshop-sheet/import",
                data={
                    "expected_revision_id": repository.workspace_domain()["revisionId"],
                },
                files=[("files", (exported_name, downloaded.content, "image/png"))],
            )
            assert imported.status_code == 201, imported.text
            assert imported.json()["variant"]["kind"] == "photoshop"

            missing_remote = await client.post(
                f"/api/v4/material-sources/{source['id']}/remote-jobs",
                    json={
                        "expectedRevisionId": repository.workspace_domain()["revisionId"],
                        "frameIndexes": [0],
                        "quality": "high",
                    "settings": {"material_type": "character"},
                },
            )
            assert missing_remote.status_code == 409
            assert "客户端设置" in missing_remote.text
    finally:
        app.state.runtime.shutdown()


@pytest.mark.anyio
async def test_material_remote_job_is_orchestrated_by_the_local_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROTOWEAVE_REMOTE_MATTING_URL", "http://192.168.1.40:8443")
    settings = Settings(data_root=tmp_path / "remote-data", runtime_root=tmp_path, require_session_token=False)
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "remote-workspace", "Remote Material")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"remote-api-source")
    frame = fixture / "000000.png"
    Image.new("RGBA", (8, 6), (20, 30, 40, 255)).save(frame)
    logical = frame.relative_to(repository.root).as_posix()
    source = repository.create_material_source(
        character["id"], "Remote Source", video.relative_to(repository.root).as_posix(), [logical],
        metadata={"fps": 24.0, "durationSeconds": 1 / 24, "frameCount": 1, "width": 8, "height": 6, "color": {}, "warnings": []},
        frame_metadata=[{"linearPath": logical, "ptsUs": 0, "durationUs": 41_667, "width": 8, "height": 6}],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )

    class FakeClient:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def submit(self, _prepared):
            return SimpleNamespace(jobId="remote-1", state=RemoteJobState.COMPLETED, progress=1.0)

        async def download_result(self, _job_id, _destination):
            return SimpleNamespace()

    monkeypatch.setattr("backend.app.jobs.RemoteMattingClient", FakeClient)
    monkeypatch.setattr("backend.app.jobs.prepare_remote_submission", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        "backend.app.jobs.publish_remote_result",
        lambda *_args, **_kwargs: {"id": "mvar-remote-test"},
    )
    app.state.jobs.start()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45003))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            created = await client.post(
                f"/api/v4/material-sources/{source['id']}/remote-jobs",
                    json={
                        "expectedRevisionId": repository.workspace_domain()["revisionId"],
                        "frameIndexes": [0],
                        "quality": "high",
                    "settings": {"material_type": "character"},
                },
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["id"]
            for _ in range(100):
                jobs = (await client.get("/api/v4/jobs")).json()
                job = next(item for item in jobs if item["id"] == job_id)
                if job["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "completed", job
            assert job["result"]["remote"] == {"variantId": "mvar-remote-test", "quality": "high"}
    finally:
        app.state.jobs.stop()
        app.state.runtime.shutdown()
