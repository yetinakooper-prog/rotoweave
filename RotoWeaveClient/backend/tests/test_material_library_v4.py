from __future__ import annotations

from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.material_library import MaterialLibrary
from backend.app.workspace_format import WorkspaceFormatError, WorkspaceRevisionConflict
from backend.app.workspace_session import WorkspaceSessionManager


def _fake_probe(_: Path, __: Settings) -> dict[str, object]:
    return {
        "fps": 24.0,
        "duration": 2 / 24,
        "frame_count": 2,
        "width": 8,
        "height": 6,
        "codec": "fixture",
        "format": "fixture",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
    }


def _fake_extract(
    _source: Path,
    output_dir: Path,
    thumb_dir: Path,
    _metadata: dict[str, object],
    _target_fps: float | None,
    _start_time: float,
    _end_time: float | None,
    _settings: Settings,
    report,
    check_control,
    selected_timeline=None,
):
    del selected_timeline
    output_dir.mkdir(parents=True)
    thumb_dir.mkdir(parents=True)
    linear_dir = output_dir.parent / "source_linear"
    linear_dir.mkdir(parents=True)
    records = []
    for index in range(2):
        check_control()
        image = np.full((6, 8, 3), 40 + index * 60, dtype=np.uint8)
        preview = output_dir / f"{index:06d}.png"
        thumbnail = thumb_dir / f"{index:06d}.jpg"
        linear = linear_dir / f"{index:06d}.exr"
        assert cv2.imwrite(str(preview), image)
        assert cv2.imwrite(str(thumbnail), image)
        linear.write_bytes(f"linear-{index}".encode())
        records.append(
            {
                "source_path": str(preview),
                "linear_source_path": str(linear),
                "pts_us": round(index / 24 * 1_000_000),
                "duration_us": round(1_000_000 / 24),
                "width": 8,
                "height": 6,
                "source_color": {
                    "transfer": "bt709",
                    "primaries": "bt709",
                    "matrix": "bt709",
                    "range": "tv",
                    "workingSpace": "linear-rec709",
                },
            }
        )
    report("thumbnails", 1.0, None)
    return records, {
        "fps": 24.0,
        "duration": 2 / 24,
        "extractor": "fixture",
        "source_authority": "linear-rec709-half-exr",
    }


def _open(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "runtime", runtime_root=tmp_path)
    session = WorkspaceSessionManager(settings)
    session.create(tmp_path / "workspace", "Materials 4")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    return settings, session, repository, character


def test_import_publishes_source_frames_and_runtime_thumbnails_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, repository, character = _open(tmp_path)
    monkeypatch.setattr("backend.app.material_library.probe_video", _fake_probe)
    monkeypatch.setattr("backend.app.material_library.extract_frames", _fake_extract)
    source_video = tmp_path / "outside.mp4"
    source_video.write_bytes(b"stable-source-video")
    library = MaterialLibrary(repository, settings, Path(session.runtime_root))

    result = library.import_video(character["id"], source_video, "Idle")
    source = result["source"]

    assert result["duplicate"] is False
    assert source["metadata"]["frameCount"] == 2
    assert len(source["frames"]) == 2
    assert all((repository.root / item["path"]).is_file() for item in source["frames"])
    assert all(
        (repository.root / item["linear"]["path"]).is_file()
        for item in source["frames"]
    )
    assert library.thumbnail_path(source["id"], 0).is_file()
    assert not (Path(session.runtime_root) / "material-import" / source["id"]).exists()
    assert repository.validate(full_hash=True)["fullHash"] is True

    duplicate = library.import_video(character["id"], source_video, "Duplicate")
    assert duplicate["duplicate"] is True
    assert duplicate["source"]["id"] == source["id"]
    assert len(repository.workspace_domain()["materialSources"]) == 1

    changed_video = tmp_path / "changed.mp4"
    changed_video.write_bytes(b"different-video")
    with pytest.raises(WorkspaceRevisionConflict):
        library.import_video(
            character["id"],
            changed_video,
            "Stale",
            expected_revision_id="rev_stale",
        )
    source_root = repository.root / "materials" / "sources"
    assert [item.name for item in source_root.iterdir()] == [source["id"]]
    assert not list((Path(session.runtime_root) / "material-import").glob("*"))


def test_all_variant_kinds_are_immutable_mapped_and_cleanup_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, repository, character = _open(tmp_path)
    monkeypatch.setattr("backend.app.material_library.probe_video", _fake_probe)
    monkeypatch.setattr("backend.app.material_library.extract_frames", _fake_extract)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"variant-source")
    source = MaterialLibrary(
        repository, settings, Path(session.runtime_root)
    ).import_video(character["id"], video, "Run")["source"]

    variants = []
    for kind in ("basic", "high", "ultra", "photoshop"):
        frame_paths = []
        for index in range(2):
            path = tmp_path / f"{kind}-{index}.png"
            assert cv2.imwrite(
                str(path), np.full((6, 8, 4), 30 + index, dtype=np.uint8)
            )
            frame_paths.append(str(path))
        variants.append(
            repository.publish_material_variant(
                source["id"],
                kind,
                frame_paths,
                {"kind": kind, "quality": 1},
                expected_revision_id=repository.workspace_domain()["revisionId"],
            )
        )

    assert [item["kind"] for item in variants] == [
        "basic",
        "high",
        "ultra",
        "photoshop",
    ]
    assert len({item["settingsSha256"] for item in variants}) == 4
    for variant in variants:
        assert [item["sourceFrameId"] for item in variant["frames"]] == [
            item["id"] for item in source["frames"]
        ]

    before_dirs = set((repository.root / "materials" / "variants").iterdir())
    with pytest.raises(WorkspaceRevisionConflict):
        repository.publish_material_variant(
            source["id"],
            "basic",
            [str(tmp_path / "basic-0.png"), str(tmp_path / "basic-1.png")],
            {"retry": True},
            expected_revision_id="rev_stale",
        )
    assert set((repository.root / "materials" / "variants").iterdir()) == before_dirs

    action = repository.create_domain_action(
        character["id"],
        "Idle",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.replace_action_frame_refs(
        action["id"],
        [
            {
                "variantId": variants[0]["id"],
                "frameId": variants[0]["frames"][0]["id"],
            }
        ],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    with pytest.raises(WorkspaceFormatError, match="显式"):
        repository.cleanup_material_variant(
            variants[1]["id"],
            explicit=False,
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )
    with pytest.raises(WorkspaceFormatError, match="仍被动作引用"):
        repository.cleanup_material_variant(
            variants[0]["id"],
            explicit=True,
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )
    cleanup = repository.cleanup_material_variant(
        variants[1]["id"],
        explicit=True,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert cleanup["referenceCount"] == 0
    assert repository.remove_domain_asset_files(cleanup["assetPaths"]) > 0
    assert not any((repository.root / path).exists() for path in cleanup["assetPaths"])
    assert repository.get_material_variant(variants[1]["id"]) is None


@pytest.mark.anyio
async def test_material_api_import_preview_playback_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.app.material_library.probe_video", _fake_probe)
    monkeypatch.setattr("backend.app.material_library.extract_frames", _fake_extract)
    settings = Settings(
        data_root=tmp_path / "data",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Material API")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 44001))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        character_response = await client.post(
            "/api/v4/domain/characters", json={"name": "Hero"}
        )
        assert character_response.status_code == 201
        character = character_response.json()["character"]
        imported = await client.post(
            f"/api/v4/domain/characters/{character['id']}/materials/import",
            data={"display_name": "Walk"},
            files={"file": ("walk.mp4", b"api-video", "video/mp4")},
        )
        assert imported.status_code == 201, imported.text
        source = imported.json()["source"]
        video = await client.get(f"/api/v4/material-sources/{source['id']}/video")
        thumbnail = await client.get(
            f"/api/v4/material-sources/{source['id']}/frames/0/thumbnail"
        )
        assert video.status_code == 200 and video.content == b"api-video"
        assert thumbnail.status_code == 200 and thumbnail.headers["content-type"] == "image/jpeg"
        repository = app.state.workspace_session.require_repository()
        variants = []
        for kind in ("basic", "photoshop"):
            rgba_paths = []
            emission_paths = []
            for index in range(2):
                rgba = tmp_path / f"delete-{kind}-{index}.png"
                emission = tmp_path / f"delete-{kind}-{index}-emission.png"
                assert cv2.imwrite(str(rgba), np.full((6, 8, 4), 60 + index, dtype=np.uint8))
                assert cv2.imwrite(str(emission), np.full((6, 8, 4), 90 + index, dtype=np.uint8))
                rgba_paths.append(str(rgba))
                emission_paths.append(str(emission))
            variants.append(repository.publish_material_variant(
                source["id"],
                kind,
                rgba_paths,
                {"kind": kind},
                emission_paths=emission_paths,
                expected_revision_id=repository.workspace_domain()["revisionId"],
            ))
        managed_paths = [
            repository.root / frame[asset]["path"]
            for variant in variants
            for frame in variant["frames"]
            for asset in ("emission",)
        ] + [
            repository.root / frame["path"]
            for variant in variants
            for frame in variant["frames"]
        ]
        assert all(path.is_file() for path in managed_paths)
        deleted = await client.delete(
            f"/api/v4/material-sources/{source['id']}",
            params={
                "explicit": "true",
                "expected_revision_id": repository.workspace_domain()["revisionId"],
            },
        )
        assert deleted.status_code == 200, deleted.text
        deletion = deleted.json()["deletion"]
        assert deletion["reclaimedBytes"] > 0
        assert deletion["removedVariantCount"] == 2
        assert deletion["removedVariantIds"] == [item["id"] for item in variants]
        assert deletion["referenceCount"] == 0
        assert not any(path.exists() for path in managed_paths)
        assert not list((tmp_path / "workspace" / "materials" / "sources").glob("*"))
        assert not (tmp_path / "material-thumbnails" / source["id"]).exists()


def test_source_delete_is_atomic_when_any_variant_is_referenced_or_revision_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, repository, character = _open(tmp_path)
    monkeypatch.setattr("backend.app.material_library.probe_video", _fake_probe)
    monkeypatch.setattr("backend.app.material_library.extract_frames", _fake_extract)
    video = tmp_path / "atomic-source.mp4"
    video.write_bytes(b"atomic-source")
    source = MaterialLibrary(repository, settings, Path(session.runtime_root)).import_video(
        character["id"], video, "Atomic"
    )["source"]
    variants = []
    for kind in ("basic", "high"):
        paths = []
        for index in range(2):
            path = tmp_path / f"atomic-{kind}-{index}.png"
            assert cv2.imwrite(str(path), np.full((6, 8, 4), 40 + index, dtype=np.uint8))
            paths.append(str(path))
        variants.append(repository.publish_material_variant(
            source["id"], kind, paths, {"kind": kind},
            expected_revision_id=repository.workspace_domain()["revisionId"],
        ))
    action = repository.create_domain_action(
        character["id"], "Referenced",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.replace_action_frame_refs(
        action["id"],
        [{"variantId": variants[1]["id"], "frameId": variants[1]["frames"][0]["id"]}],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    before = repository.workspace_domain()
    files = [repository.root / item["path"] for variant in variants for item in variant["frames"]]
    with pytest.raises(WorkspaceFormatError, match="仍被动作引用"):
        repository.delete_material_source(
            source["id"], explicit=True, expected_revision_id=before["revisionId"]
        )
    assert repository.workspace_domain() == before
    assert all(path.is_file() for path in files)

    with pytest.raises(WorkspaceRevisionConflict):
        repository.delete_material_source(
            source["id"], explicit=True, expected_revision_id="rev_stale"
        )
    assert repository.workspace_domain() == before
    assert all(path.is_file() for path in files)
