from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.workspace_format import (
    WORKSPACE_DOMAIN,
    WorkspaceChangedError,
    WorkspaceFormatError,
    WorkspaceRevisionConflict,
    atomic_write_json,
    finalize_aggregate,
    new_workspace_manifest,
    validate_workspace_domain,
    validate_workspace_manifest,
)
from backend.app.workspace_session import WorkspaceSessionManager


def _open(tmp_path: Path) -> tuple[WorkspaceSessionManager, Path]:
    root = tmp_path / "workspace-v4"
    session = WorkspaceSessionManager(
        Settings(data_root=tmp_path / "runtime", runtime_root=tmp_path)
    )
    session.create(root, "Workspace 4")
    return session, root


def _asset(root: Path, relative: str, payload: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return relative


def _revision(repository) -> str:
    return str(repository.workspace_domain()["revisionId"])


def test_format3_domain_expresses_sources_variants_actions_and_export_state(
    tmp_path: Path,
) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    video = _asset(root, "materials/source/video.mp4", b"video-v4")
    source_frames = [
        _asset(root, f"materials/source/frames/{index:06d}.png", f"src-{index}".encode())
        for index in range(2)
    ]
    basic_frames = [
        _asset(root, f"materials/variants/basic/{index:06d}.png", f"basic-{index}".encode())
        for index in range(2)
    ]
    high_frames = [
        _asset(root, f"materials/variants/high/{index:06d}.png", f"high-{index}".encode())
        for index in range(2)
    ]
    atlas = _asset(root, "delivery/current/atlas-00.png", b"atlas")

    character = repository.create_domain_character(
        "角色一",
        expected_revision_id=_revision(repository),
    )
    source = repository.create_material_source(
        character["id"],
        "待机视频",
        video,
        source_frames,
        expected_revision_id=_revision(repository),
    )
    basic = repository.append_material_variant(
        source["id"],
        "basic",
        basic_frames,
        {"route": "character", "screenColor": "#00ff00"},
        expected_revision_id=_revision(repository),
    )
    high = repository.append_material_variant(
        source["id"],
        "high",
        high_frames,
        {"remoteProtocol": 1, "quality": "high"},
        expected_revision_id=_revision(repository),
    )
    action = repository.create_domain_action(
        character["id"],
        "待机",
        expected_revision_id=_revision(repository),
    )
    action = repository.replace_action_frame_refs(
        action["id"],
        [
            {
                "variantId": basic["id"],
                "frameId": basic["frames"][0]["id"],
                "durationSeconds": 1 / 24,
                "transform": {
                    "position": {"x": 12.0, "y": -4.0},
                    "scale": {"x": 1.25, "y": 1.25},
                    "rotationDegrees": 5.0,
                    "color": "#f0e0d0",
                    "opacity": 0.75,
                    "shadow": {
                        "enabled": True,
                        "color": "#101010",
                        "opacity": 0.4,
                        "offset": {"x": 2.0, "y": 3.0},
                        "scale": {"x": 1.2, "y": 0.6},
                    },
                },
            }
        ],
        expected_revision_id=_revision(repository),
    )
    export_state = repository.set_domain_export_state(
        character["id"],
        "current",
        current_atlas_path=atlas,
        expected_revision_id=_revision(repository),
    )

    domain = repository.workspace_domain()
    assert domain["workspaceFormatVersion"] == 3
    assert domain["materialVariants"][0] == basic
    assert domain["materialVariants"][1] == high
    assert action["frameRefs"][0]["variantId"] == basic["id"]
    assert action["frameRefs"][0]["durationSeconds"] == pytest.approx(1 / 24)
    assert action["frameRefs"][0]["transform"]["shadow"]["enabled"] is True
    assert export_state["currentAtlas"]["path"] == atlas
    validation = repository.validate(full_hash=True)
    assert validation["fullHash"] is True
    assert validation["referencedFiles"] == 8
    assert (root / WORKSPACE_DOMAIN).is_file()
    assert not list(root.rglob("*.sqlite3"))
    assert session.runtime_root.is_relative_to(tmp_path / "runtime")


def test_domain_schema_revision7_persists_calibration_shadow_and_delivery(tmp_path: Path) -> None:
    session, _ = _open(tmp_path); repository = session.require_repository()
    character = repository.create_domain_character("校准角色", expected_revision_id=_revision(repository))
    assert repository.workspace_domain()["domainSchemaRevision"] == 7
    assert character["calibration"]["sizeProfiles"][0]["width"] == 512
    assert character["calibration"]["sizeProfiles"][0]["unitMode"] == "pixels"
    assert character["calibration"]["sizeProfiles"][0]["presetId"] is None
    assert character["calibration"]["pixelsPerUnit"] == 100
    changed = repository.update_domain_character_settings(character["id"], {
        "calibration": {"alignmentHorizonY": -12.5, "shadowStandardY": 8.0},
        "shadow": {"enabled": True, "color": "#123456", "baseOpacity": 0.42, "lightAngleDegrees": 160},
        "delivery": {"atlas": {"maxSize": 8192, "padding": 4, "extrude": 2, "framePadding": 3}},
    }, expected_revision_id=_revision(repository))
    assert changed["calibration"]["alignmentHorizonY"] == -12.5
    assert changed["shadow"]["color"] == "#123456"
    assert changed["delivery"]["atlas"] == {"maxSize": 8192, "padding": 4, "extrude": 2, "framePadding": 3}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("padding", -1),
        ("padding", 129),
        ("extrude", 33),
        ("framePadding", 257),
        ("framePadding", 1.5),
        ("padding", True),
    ],
)
def test_atlas_spacing_settings_require_bounded_integers(
    tmp_path: Path, key: str, value: object
) -> None:
    session, _ = _open(tmp_path)
    repository = session.require_repository()
    repository.create_domain_character("Atlas validation")
    domain = repository.workspace_domain()
    domain["characters"][0]["delivery"]["atlas"][key] = value
    finalized, _ = finalize_aggregate(domain)
    with pytest.raises(WorkspaceFormatError, match="图集间距"):
        validate_workspace_domain(finalized)


def test_shared_size_preset_changes_never_mutate_character_snapshot(tmp_path: Path) -> None:
    session, _ = _open(tmp_path)
    repository = session.require_repository()
    preset = repository.create_size_profile(
        "角色标准", 5.12, 7.68, unit_mode="pixels"
    )
    character = repository.create_domain_character(
        "快照角色", expected_revision_id=_revision(repository)
    )
    local_profile_id = character["calibration"]["activeSizeProfileId"]
    snapshot = {
        "id": local_profile_id,
        "name": preset["name"],
        "presetId": preset["id"],
        "unitMode": "pixels",
        "width": 512,
        "height": 768,
    }
    character = repository.update_domain_character_settings(
        character["id"],
        {"calibration": {"sizeProfiles": [snapshot]}},
        expected_revision_id=_revision(repository),
    )

    repository.update_size_profile(
        preset["id"], {"name": "角色标准新版", "width_world": 6.4}
    )
    after_update = next(
        item
        for item in repository.workspace_domain()["characters"]
        if item["id"] == character["id"]
    )
    assert after_update["calibration"]["sizeProfiles"] == [snapshot]

    assert repository.delete_size_profile(preset["id"]) is True
    after_delete = next(
        item
        for item in repository.workspace_domain()["characters"]
        if item["id"] == character["id"]
    )
    assert after_delete["calibration"]["sizeProfiles"] == [snapshot]


@pytest.mark.parametrize("revision", [1, 2, 3, 4, 5, 6])
def test_noncurrent_domain_revision_is_rejected_without_migration(
    tmp_path: Path, revision: int,
) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    current = repository.workspace_domain()
    session.shutdown()

    invalid = json.loads(json.dumps(current))
    invalid["domainSchemaRevision"] = revision
    invalid, _ = finalize_aggregate(invalid, previous=current)
    atomic_write_json(root / WORKSPACE_DOMAIN, invalid)

    reopened = WorkspaceSessionManager(
        Settings(data_root=tmp_path / "runtime-reopened", runtime_root=tmp_path)
    )
    with pytest.raises(WorkspaceFormatError):
        reopened.open(root)

def test_variants_are_append_only_and_referenced_versions_cannot_be_cleaned(
    tmp_path: Path,
) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    character = repository.create_domain_character(
        "角色",
        expected_revision_id=_revision(repository),
    )
    source = repository.create_material_source(
        character["id"],
        "视频",
        _asset(root, "materials/source.mp4", b"video"),
        [_asset(root, "materials/source-0.png", b"source")],
        expected_revision_id=_revision(repository),
    )
    first = repository.append_material_variant(
        source["id"],
        "basic",
        [_asset(root, "materials/basic-0.png", b"basic")],
        {"quality": "basic"},
        expected_revision_id=_revision(repository),
    )
    second = repository.append_material_variant(
        source["id"],
        "photoshop",
        [_asset(root, "materials/ps-0.png", b"ps")],
        {"sheetSha256": "a" * 64},
        expected_revision_id=_revision(repository),
    )
    action = repository.create_domain_action(
        character["id"],
        "动作",
        expected_revision_id=_revision(repository),
    )
    repository.replace_action_frame_refs(
        action["id"],
        [{
            "variantId": first["id"],
            "frameId": first["frames"][0]["id"],
        }],
        expected_revision_id=_revision(repository),
    )

    with pytest.raises(WorkspaceFormatError, match="仍被动作引用"):
        repository.cleanup_material_variant(
            first["id"],
            explicit=True,
            expected_revision_id=_revision(repository),
        )
    with pytest.raises(WorkspaceFormatError, match="显式清理"):
        repository.cleanup_material_variant(
            second["id"],
            explicit=False,
            expected_revision_id=_revision(repository),
        )
    cleaned = repository.cleanup_material_variant(
        second["id"],
        explicit=True,
        expected_revision_id=_revision(repository),
    )
    assert cleaned["assetPaths"] == ["materials/ps-0.png"]
    assert [item["id"] for item in repository.workspace_domain()["materialVariants"]] == [
        first["id"]
    ]
    assert repository.workspace_domain()["materialVariants"][0]["settings"] == {
        "quality": "basic"
    }


def test_new_variant_atomically_migrates_same_source_action_refs(tmp_path: Path) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    character = repository.create_domain_character(
        "迁移角色", expected_revision_id=_revision(repository)
    )
    source = repository.create_material_source(
        character["id"],
        "动作素材",
        _asset(root, "materials/migrate/source.mp4", b"video"),
        [
            _asset(root, "materials/migrate/source-0.png", b"source-0"),
            _asset(root, "materials/migrate/source-1.png", b"source-1"),
        ],
        expected_revision_id=_revision(repository),
    )
    first = repository.append_material_variant(
        source["id"],
        "basic",
        [
            _asset(root, "materials/migrate/basic-0.png", b"basic-0"),
            _asset(root, "materials/migrate/basic-1.png", b"basic-1"),
        ],
        {"quality": "basic"},
        expected_revision_id=_revision(repository),
    )
    action = repository.create_domain_action(
        character["id"], "迁移动作", expected_revision_id=_revision(repository)
    )
    saved = repository.replace_action_frame_refs(
        action["id"],
        [
            {
                "id": "afrm_keep_identity",
                "variantId": first["id"],
                "frameId": first["frames"][1]["id"],
                "durationSeconds": 0.25,
                "enabled": False,
                "transform": {
                    "position": {"x": 7, "y": -3},
                    "scale": {"x": 1.2, "y": 0.8},
                    "rotationDegrees": 11,
                    "color": "#abcdef",
                    "opacity": 0.7,
                    "shadow": {
                        "enabled": None,
                        "color": None,
                        "opacity": None,
                        "offset": {"x": 2, "y": 4},
                        "scale": {"x": 1.1, "y": 0.6},
                    },
                },
            }
        ],
        expected_revision_id=_revision(repository),
    )
    previous_ref = saved["frameRefs"][0]

    latest = repository.append_material_variant(
        source["id"],
        "photoshop",
        [
            _asset(root, "materials/migrate/latest-0.png", b"latest-0"),
            _asset(root, "materials/migrate/latest-1.png", b"latest-1"),
        ],
        {"sheetId": "sheet"},
        expected_revision_id=_revision(repository),
    )
    migrated = repository.get_domain_action(action["id"])["frameRefs"][0]
    assert latest["migration"] == {
        "actionFrameCount": 1,
        "actionIds": [action["id"]],
    }
    assert migrated["id"] == previous_ref["id"]
    assert migrated["variantId"] == latest["id"]
    assert migrated["frameId"] == latest["frames"][1]["id"]
    assert migrated["durationSeconds"] == previous_ref["durationSeconds"]
    assert migrated["enabled"] is False
    assert migrated["transform"] == previous_ref["transform"]


def test_partial_variant_migrates_only_covered_action_refs_atomically(tmp_path: Path) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    character = repository.create_domain_character("部分迁移", expected_revision_id=_revision(repository))
    source = repository.create_material_source(
        character["id"],
        "两帧素材",
        _asset(root, "materials/partial/source.mp4", b"video"),
        [
            _asset(root, "materials/partial/source-0.png", b"source-0"),
            _asset(root, "materials/partial/source-1.png", b"source-1"),
        ],
        expected_revision_id=_revision(repository),
    )
    first = repository.append_material_variant(
        source["id"],
        "basic",
        [
            _asset(root, "materials/partial/basic-0.png", b"basic-0"),
            _asset(root, "materials/partial/basic-1.png", b"basic-1"),
        ],
        {},
        expected_revision_id=_revision(repository),
    )
    action = repository.create_domain_action(
        character["id"], "混合引用", expected_revision_id=_revision(repository)
    )
    action = repository.replace_action_frame_refs(
        action["id"],
        [
            {"id": "afrm_unselected", "variantId": first["id"], "frameId": first["frames"][0]["id"]},
            {"id": "afrm_selected", "variantId": first["id"], "frameId": first["frames"][1]["id"]},
        ],
        expected_revision_id=_revision(repository),
    )
    partial = repository.append_material_variant(
        source["id"],
        "high",
        [_asset(root, "materials/partial/high-1.png", b"high-1")],
        {},
        source_frame_ids=[source["frames"][1]["id"]],
        expected_revision_id=_revision(repository),
    )
    refs = repository.get_domain_action(action["id"])["frameRefs"]
    assert partial["migration"] == {"actionFrameCount": 1, "actionIds": [action["id"]]}
    assert refs[0]["id"] == "afrm_unselected"
    assert refs[0]["variantId"] == first["id"]
    assert refs[0]["frameId"] == first["frames"][0]["id"]
    assert refs[1]["id"] == "afrm_selected"
    assert refs[1]["variantId"] == partial["id"]
    assert refs[1]["frameId"] == partial["frames"][0]["id"]

    before = repository.workspace_domain()
    with pytest.raises(WorkspaceFormatError, match="不属于素材源"):
        repository.append_material_variant(
            source["id"],
            "ultra",
            [_asset(root, "materials/partial/invalid.png", b"invalid")],
            {},
            source_frame_ids=["mfrm_missing"],
            expected_revision_id=before["revisionId"],
        )
    assert repository.workspace_domain() == before


def test_character_rename_and_explicit_delete_cascade_are_atomic(tmp_path: Path) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    character = repository.create_domain_character(
        "旧名称", expected_revision_id=_revision(repository)
    )
    renamed = repository.update_domain_character(
        character["id"],
        name="新名称",
        expected_revision_id=_revision(repository),
    )
    assert renamed["name"] == "新名称"
    source = repository.create_material_source(
        character["id"],
        "素材",
        _asset(root, "materials/delete/video.mp4", b"video"),
        [_asset(root, "materials/delete/source.png", b"source")],
        expected_revision_id=_revision(repository),
    )
    variant = repository.append_material_variant(
        source["id"],
        "basic",
        [_asset(root, "materials/delete/basic.png", b"basic")],
        {"quality": "basic"},
        expected_revision_id=_revision(repository),
    )
    action = repository.create_domain_action(
        character["id"], "动作", expected_revision_id=_revision(repository)
    )
    repository.replace_action_frame_refs(
        action["id"],
        [{"variantId": variant["id"], "frameId": variant["frames"][0]["id"]}],
        expected_revision_id=_revision(repository),
    )

    stale = _revision(repository)
    result = repository.delete_domain_character(
        character["id"], expected_revision_id=stale
    )
    assert result["removed"]["name"] == "新名称"
    assert set(result["assetPaths"]) == {
        "materials/delete/video.mp4",
        "materials/delete/source.png",
        "materials/delete/basic.png",
    }
    domain = repository.workspace_domain()
    assert domain["characters"] == []
    assert domain["actions"] == []
    assert domain["materialSources"] == []
    assert domain["materialVariants"] == []
    with pytest.raises(WorkspaceRevisionConflict):
        repository.update_domain_character(
            character["id"], name="冲突", expected_revision_id=stale
        )


def test_domain_revision_conflict_and_external_change_never_overwrite(
    tmp_path: Path,
) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    stale = _revision(repository)
    repository.create_domain_character("角色一", expected_revision_id=stale)
    with pytest.raises(WorkspaceRevisionConflict, match="已变化"):
        repository.create_domain_character("角色二", expected_revision_id=stale)

    domain_path = root / WORKSPACE_DOMAIN
    payload = json.loads(domain_path.read_text(encoding="utf-8"))
    payload["externalMarker"] = "svn-update"
    external, _ = finalize_aggregate(payload, previous=payload)
    atomic_write_json(domain_path, external)
    with pytest.raises(WorkspaceChangedError, match="版本控制"):
        repository.create_domain_character(
            "角色三",
            expected_revision_id=_revision(repository),
        )


def test_format2_is_explicitly_rejected_without_migration() -> None:
    manifest = new_workspace_manifest("legacy")
    manifest["workspaceFormatVersion"] = 2
    legacy, _ = finalize_aggregate(manifest)
    with pytest.raises(WorkspaceFormatError, match="不迁移或读取格式 2"):
        validate_workspace_manifest(legacy)


def test_domain_atomic_write_failure_keeps_previous_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, root = _open(tmp_path)
    repository = session.require_repository()
    domain_path = root / WORKSPACE_DOMAIN
    before = domain_path.read_bytes()
    before_revision = _revision(repository)

    def fail_write(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(
        "backend.app.workspace_repository.atomic_write_json",
        fail_write,
    )
    with pytest.raises(OSError, match="injected"):
        repository.create_domain_character(
            "不会发布",
            expected_revision_id=before_revision,
        )
    assert domain_path.read_bytes() == before


def test_runtime_schema4_is_external_and_rebuildable(tmp_path: Path) -> None:
    session, root = _open(tmp_path)
    runtime_database = session.runtime_root / "runtime.sqlite3"
    connection = sqlite3.connect(runtime_database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        } == {"runtime_metadata", "jobs"}
        connection.execute("CREATE TABLE retired_cache(value TEXT)")
        connection.commit()
    finally:
        connection.close()
    session.shutdown()

    session.open(root)
    connection = sqlite3.connect(session.runtime_root / "runtime.sqlite3")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        } == {"runtime_metadata", "jobs"}
    finally:
        connection.close()
    assert not list(root.rglob("*.sqlite3"))
