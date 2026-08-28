from __future__ import annotations

import json
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.workspace_format import (
    WORKSPACE_DOMAIN,
    WorkspaceFormatError,
    atomic_write_json,
    finalize_aggregate,
)
from backend.app.workspace_session import WorkspaceSessionManager


def _domain_fixture(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "runtime",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    session = WorkspaceSessionManager(settings)
    session.create(tmp_path / "workspace", "Action 4")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    fixtures = repository.root / "fixtures"
    fixtures.mkdir()
    video = fixtures / "source.mp4"
    video.write_bytes(b"action-source")
    frame_paths = []
    frame_metadata = []
    absolute_frames = []
    for index in range(3):
        image = np.zeros((10, 12, 4), dtype=np.uint8)
        image[2:9, 3:10] = (20 + index, 120, 230, 255)
        path = fixtures / f"{index:06d}.png"
        assert cv2.imwrite(str(path), image)
        absolute_frames.append(str(path))
        logical = path.relative_to(repository.root).as_posix()
        frame_paths.append(logical)
        frame_metadata.append(
            {
                "linearPath": logical,
                "ptsUs": index * 41_667,
                "durationUs": 41_667,
                "width": 12,
                "height": 10,
            }
        )
    source = repository.create_material_source(
        character["id"],
        "Action Source",
        video.relative_to(repository.root).as_posix(),
        frame_paths,
        metadata={
            "fps": 24.0,
            "durationSeconds": 3 / 24,
            "frameCount": 3,
            "width": 12,
            "height": 10,
            "color": {
                "transfer": "bt709",
                "primaries": "bt709",
                "matrix": "bt709",
                "range": "tv",
            },
            "warnings": [],
        },
        frame_metadata=frame_metadata,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    variant = repository.publish_material_variant(
        source["id"],
        "basic",
        absolute_frames,
        {"quality": "basic"},
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    return settings, session, repository, character, source, variant


def _ref(variant: dict, index: int, **changes):
    value = {
        "variantId": variant["id"],
        "frameId": variant["frames"][index]["id"],
        "durationSeconds": 1 / 24,
        "transform": {
            "position": {"x": 0, "y": 0},
            "scale": {"x": 1, "y": 1},
            "rotationDegrees": 0,
            "color": "#ffffff",
            "opacity": 1,
            "shadow": {
                "enabled": False,
                "color": "#000000",
                "opacity": 0,
                "offset": {"x": 0, "y": 0},
                "scale": {"x": 1, "y": 1},
            },
        },
    }
    value.update(changes)
    return value


def test_action_crud_batch_refs_and_variant_lifecycle(tmp_path: Path) -> None:
    _, _, repository, character, _, variant = _domain_fixture(tmp_path)
    action = repository.create_domain_action(
        character["id"],
        "Run",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert action["loop"] is True and action["frameRefs"] == []
    with pytest.raises(WorkspaceFormatError, match="重复"):
        repository.create_domain_action(
            character["id"],
            "run",
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )

    action = repository.append_action_frame_refs(
        action["id"],
        [_ref(variant, 0)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    action = repository.append_action_frame_refs(
        action["id"],
        [_ref(variant, 1), _ref(variant, 2)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert len(action["frameRefs"]) == 3
    assert repository.material_variant_reference_count(variant["id"]) == 3

    changed = [_ref(variant, 2), _ref(variant, 0)]
    changed[0]["durationSeconds"] = 0.125
    changed[0]["transform"] = {
        "position": {"x": 12.5, "y": -4},
        "scale": {"x": 1.5, "y": 0.75},
        "rotationDegrees": 18,
        "color": "#80c0ff",
        "opacity": 0.6,
        "shadow": {
            "enabled": True,
            "color": "#112233",
            "opacity": 0.45,
            "offset": {"x": 3, "y": -2},
            "scale": {"x": 1.2, "y": 0.8},
        },
    }
    saved = repository.replace_action_frame_refs(
        action["id"],
        changed,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert saved["frameRefs"][0]["durationSeconds"] == 0.125
    assert saved["frameRefs"][0]["transform"]["shadow"]["enabled"] is True
    assert [item["frameId"] for item in saved["frameRefs"]] == [
        variant["frames"][2]["id"],
        variant["frames"][0]["id"],
    ]

    renamed = repository.update_domain_action(
        action["id"],
        name="Sprint",
        loop=False,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert renamed["name"] == "Sprint" and renamed["loop"] is False
    with pytest.raises(WorkspaceFormatError, match="引用"):
        repository.cleanup_material_variant(
            variant["id"],
            explicit=True,
            expected_revision_id=repository.workspace_domain()["revisionId"],
        )
    removed = repository.delete_domain_action(
        action["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert removed["name"] == "Sprint"
    assert repository.material_variant_reference_count(variant["id"]) == 0
    assert repository.cleanup_material_variant(
        variant["id"],
        explicit=True,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )["referenceCount"] == 0


def test_action_transform_validation_is_atomic(tmp_path: Path) -> None:
    _, _, repository, character, _, variant = _domain_fixture(tmp_path)
    action = repository.create_domain_action(
        character["id"],
        "Idle",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    revision = repository.workspace_domain()["revisionId"]
    invalid = _ref(variant, 0)
    invalid["transform"]["scale"]["x"] = 0
    with pytest.raises(WorkspaceFormatError, match="变换数值"):
        repository.replace_action_frame_refs(
            action["id"], [invalid], expected_revision_id=revision
        )
    assert repository.workspace_domain()["revisionId"] == revision
    assert repository.get_domain_action(action["id"])["frameRefs"] == []


@pytest.mark.anyio
async def test_action_api_save_reset_and_revision_conflict(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path / "runtime",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Action API")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    # Build the source and variant in the app-owned repository.
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"api-action")
    image = np.zeros((8, 8, 4), dtype=np.uint8)
    image[2:7, 2:7] = (20, 120, 230, 255)
    frame = fixture / "000000.png"
    assert cv2.imwrite(str(frame), image)
    logical = frame.relative_to(repository.root).as_posix()
    source = repository.create_material_source(
        character["id"],
        "API Source",
        video.relative_to(repository.root).as_posix(),
        [logical],
        metadata={
            "fps": 24.0,
            "durationSeconds": 1 / 24,
            "frameCount": 1,
            "width": 8,
            "height": 8,
            "color": {"transfer": "bt709", "primaries": "bt709", "matrix": "bt709", "range": "tv"},
            "warnings": [],
        },
        frame_metadata=[{"linearPath": logical, "ptsUs": 0, "durationUs": 41_667, "width": 8, "height": 8}],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    variant = repository.publish_material_variant(
        source["id"],
        "basic",
        [str(frame)],
        {"quality": "basic"},
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45020))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            initial = repository.workspace_domain()["revisionId"]
            created = await client.post(
                f"/api/v4/domain/characters/{character['id']}/actions",
                json={"name": "Jump", "expectedRevisionId": initial},
            )
            assert created.status_code == 201, created.text
            action = created.json()["action"]
            created_revision = created.json()["revisionId"]
            stale = await client.post(
                f"/api/v4/domain/characters/{character['id']}/actions",
                json={"name": "Stale", "expectedRevisionId": initial},
            )
            assert stale.status_code == 409

            appended = await client.post(
                f"/api/v4/domain/actions/{action['id']}/frames",
                json={
                    "expectedRevisionId": created_revision,
                    "frames": [
                        {
                            "variantId": variant["id"],
                            "frameId": variant["frames"][0]["id"],
                            "durationSeconds": 0.2,
                            "transform": {
                                "position": {"x": 4, "y": 5},
                                "rotationDegrees": 12,
                                "opacity": 0.7,
                            },
                        }
                    ],
                },
            )
            assert appended.status_code == 200, appended.text
            preview = await client.get(
                f"/api/v4/material-variants/{variant['id']}/frames/0"
            )
            assert preview.status_code == 200
            assert preview.headers["content-type"].startswith("image/png")
            saved = appended.json()["action"]
            assert saved["frameRefs"][0]["transform"]["position"] == {"x": 4.0, "y": 5.0}
            saved_revision = appended.json()["revisionId"]

            reset = await client.post(f"/api/v4/domain/actions/{action['id']}/reset")
            assert reset.status_code == 200
            assert reset.json()["action"] == saved
            assert reset.json()["revisionId"] == saved_revision

            normalized = await client.put(
                f"/api/v4/domain/actions/{action['id']}/frames",
                json={
                    "expectedRevisionId": saved_revision,
                    "frames": [
                        {
                            **saved["frameRefs"][0],
                            "durationSeconds": 1 / 24,
                        }
                    ],
                },
            )
            assert normalized.status_code == 200, normalized.text
            assert normalized.json()["action"]["frameRefs"][0]["durationSeconds"] == pytest.approx(1 / 24)
    finally:
        app.state.runtime.shutdown()
