from __future__ import annotations

import copy
import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from PIL import Image

from backend.app.config import Settings
from backend.app.domain_character_exporter import (
    PreparedSprite,
    SpriteSource,
    _encode_pages,
    _pack_compact,
    _pack_shelves,
    _prepare_sprites,
    _collect_sources,
    estimate_domain_character,
    export_domain_character,
    repair_domain_atlas_page,
)
from backend.app.domain_shadows import resolve_domain_action_shadows
from backend.app.main import create_app
from backend.app.workspace_format import (
    WORKSPACE_DOMAIN,
    WorkspaceFormatError,
    atomic_write_json,
    finalize_aggregate,
)
from backend.app.workspace_repository import (
    validate_character_animation_timings,
    validate_unity_delivery_archive,
)
from backend.app.workspace_session import WorkspaceSessionManager


def _frame_ref(variant: dict, index: int, scale: float, *, duration: float = 1 / 24):
    return {
        "variantId": variant["id"],
        "frameId": variant["frames"][index]["id"],
        "durationSeconds": duration,
        "transform": {
            "position": {"x": 3.5, "y": -2},
            "scale": {"x": scale, "y": scale},
            "rotationDegrees": 12,
            "color": "#80c0ff",
            "opacity": 0.75,
            "shadow": {
                "enabled": True,
                "color": "#112233",
                "opacity": 0.4,
                "offset": {"x": 2, "y": -1},
                "scale": {"x": 1.2, "y": 0.8},
            },
        },
    }


def _fixture(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "runtime",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    session = WorkspaceSessionManager(settings)
    session.create(tmp_path / "workspace", "Export 4")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    fixtures = repository.root / "fixtures"
    fixtures.mkdir()
    video = fixtures / "source.mp4"
    video.write_bytes(b"export-source")
    image_paths: list[Path] = []
    logical_paths: list[str] = []
    frame_metadata: list[dict] = []
    # Both files contain identical pixels. Deduplication must use content,
    # not the material variant/frame identity.
    pixels = np.zeros((8, 10, 4), dtype=np.uint8)
    pixels[1:7, 2:9] = (30, 150, 240, 255)
    for index in range(2):
        path = fixtures / f"{index:06d}.png"
        Image.fromarray(pixels, mode="RGBA").save(path)
        image_paths.append(path)
        logical = path.relative_to(repository.root).as_posix()
        logical_paths.append(logical)
        frame_metadata.append(
            {
                "linearPath": logical,
                "ptsUs": index * 41_667,
                "durationUs": 41_667,
                "width": 10,
                "height": 8,
            }
        )
    source = repository.create_material_source(
        character["id"],
        "Source",
        video.relative_to(repository.root).as_posix(),
        logical_paths,
        metadata={
            "fps": 24.0,
            "durationSeconds": 2 / 24,
            "frameCount": 2,
            "width": 10,
            "height": 8,
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
        [str(path) for path in image_paths],
        {"quality": "basic"},
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    action = repository.create_domain_action(
        character["id"],
        "Run",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    action = repository.replace_action_frame_refs(
        action["id"],
        [
            _frame_ref(variant, 0, 1.0, duration=0.1),
            _frame_ref(variant, 1, 2.0, duration=0.2),
            _frame_ref(variant, 0, 1.5, duration=0.3),
        ],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    return settings, session, repository, character, variant, action


def test_format3_export_deduplicates_and_uses_texture_scale_only(
    tmp_path: Path,
) -> None:
    _, _, repository, character, variant, _ = _fixture(tmp_path)
    estimate = estimate_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    result = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    archive_path = repository.root / result["archivePath"]
    validate_unity_delivery_archive(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        checksums = json.loads(archive.read("checksums.json"))
        actual_png_bytes = sum(
            len(archive.read(name)) for name in archive.namelist() if name.endswith(".png")
        )
        page_bytes = archive.read("atlases/base/00.png")

    assert manifest["formatVersion"] == 3
    assert manifest["packageShape"] == "deduplicated-atlas-v3"
    assert manifest["coordinateContract"] == "frame-transform-unity-curves-v3"
    assert manifest["character"]["defaultAnimationId"] == manifest["animations"][0]["id"]
    assert manifest["character"]["designSize"] == {
        "profileId": "default",
        "displayName": "默认",
        "sourceUnit": "pixels",
        "sourceWidth": 512.0,
        "sourceHeight": 512.0,
        "widthPixels": 512,
        "heightPixels": 512,
        "widthWorld": 5.12,
        "heightWorld": 5.12,
        "pixelsPerUnit": 100.0,
    }
    assert manifest["renderContract"]["base"]["alphaMode"] == "straight"
    assert manifest["deduplication"] == {
        "identity": "base-and-emission-sha256",
        "referencedFrames": 3,
        "uniqueSprites": 1,
        "resolutionPolicy": "maximum-texture-scale",
    }
    assert len(manifest["sprites"]) == 1
    sprite = manifest["sprites"][0]
    assert sprite["outputScale"] == 1.0
    assert sprite["rect"]["width"] == 7
    assert sprite["rect"]["height"] == 7
    assert sprite["pivot"] == {"x": pytest.approx(3 / 7), "y": 0.0}
    assert estimate["estimatedPngBytes"] == actual_png_bytes
    assert estimate["pages"] == [
        {
            "index": 0,
            "width": manifest["atlases"]["base"][0]["width"],
            "height": manifest["atlases"]["base"][0]["height"],
        }
    ]
    assert estimate["pages"][0]["width"] * estimate["pages"][0]["height"] <= 14 * 12

    with Image.open(BytesIO(page_bytes)) as page:
        rect = sprite["rect"]
        top = page.height - rect["y"] - rect["height"]
        cropped = page.crop(
            (rect["x"], top, rect["x"] + rect["width"], top + rect["height"])
        )
    reconstructed = Image.new("RGBA", (10, 8), (0, 0, 0, 0))
    try:
        local_anchor = (
            sprite["pivot"]["x"] * rect["width"],
            (1 - sprite["pivot"]["y"]) * rect["height"],
        )
        reconstructed.alpha_composite(
            cropped,
            (round(5 - local_anchor[0]), round(8 - local_anchor[1])),
        )
        with Image.open(repository.root / variant["frames"][0]["path"]) as original:
            assert reconstructed.tobytes() == original.convert("RGBA").tobytes()
    finally:
        cropped.close()
        reconstructed.close()
    assert {frame["spriteId"] for frame in manifest["animations"][0]["frames"]} == {
        sprite["id"]
    }
    assert manifest["animations"][0]["durationSeconds"] == pytest.approx(0.6)
    assert manifest["animations"][0]["frameRate"] == pytest.approx(5.0)
    assert [frame["durationSeconds"] for frame in manifest["animations"][0]["frames"]] == pytest.approx([0.1, 0.2, 0.3])
    assert manifest["animations"][0]["frames"][0]["transform"]["shadow"][
        "enabled"
    ] is True
    assert manifest["animations"][0]["frames"][0]["transform"]["position"] == {
        "x": 3.5,
        "y": -2,
    }
    assert [frame["transform"]["scale"]["x"] for frame in manifest["animations"][0]["frames"]] == [1.0, 2.0, 1.5]
    resolved_shadow = manifest["animations"][0]["frames"][0]["shadow"]
    assert set(resolved_shadow) == {
        "positionPx",
        "widthPx",
        "depthPx",
        "rotationDegrees",
        "alpha",
        "airborneRatio",
    }
    assert set(resolved_shadow["positionPx"]) == {"x", "y"}
    assert resolved_shadow["widthPx"] > resolved_shadow["depthPx"] > 0
    assert 0 <= resolved_shadow["alpha"] <= 1
    assert {item["path"] for item in checksums["files"]} == {
        "manifest.json",
        "atlases/base/00.png",
    }
    assert repository.workspace_domain()["characters"][0]["exportState"]["status"] == "current"
    repeated = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    assert repeated["sha256"] == result["sha256"]
    assert repeated["manifest"]["sprites"][0]["id"] == sprite["id"]


def test_single_frame_scale_spike_survives_shadow_stabilization_and_matches_export(
    tmp_path: Path,
) -> None:
    _, _, repository, character, _, action = _fixture(tmp_path)
    frames = copy.deepcopy(action["frameRefs"])
    frames[0]["transform"]["scale"] = {"x": 1.0, "y": 1.0}
    frames[1]["transform"]["scale"] = {"x": 2.0, "y": 1.5}
    frames[2]["transform"]["scale"] = {"x": 1.0, "y": 1.0}
    action = repository.replace_action_frame_refs(
        action["id"],
        frames,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    domain = repository.workspace_domain()
    resolved = resolve_domain_action_shadows(
        repository.root,
        domain,
        next(item for item in domain["characters"] if item["id"] == character["id"]),
        action["frameRefs"],
        loop=False,
    )
    assert resolved[1]["widthPx"] > resolved[0]["widthPx"] * 1.5
    assert resolved[1]["depthPx"] > resolved[0]["depthPx"] * 1.1

    exported = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    exported_frames = exported["manifest"]["animations"][0]["frames"]
    assert [item["shadow"]["widthPx"] for item in exported_frames] == [
        item["widthPx"] for item in resolved
    ]
    assert [item["shadow"]["depthPx"] for item in exported_frames] == [
        item["depthPx"] for item in resolved
    ]


def test_visual_scale_changes_do_not_change_atlas_estimate(tmp_path: Path) -> None:
    _, _, repository, character, _, action = _fixture(tmp_path)
    before = estimate_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    frames = copy.deepcopy(action["frameRefs"])
    frames[0]["transform"]["scale"] = {"x": 0.5, "y": 0.5}
    frames[1]["transform"]["scale"] = {"x": 8.0, "y": 8.0}
    repository.replace_action_frame_refs(
        action["id"],
        frames,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    after = estimate_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    assert after == before


def test_compact_packing_is_deterministic_and_reuses_row_holes() -> None:
    dimensions = [(80, 20), (24, 60), (24, 60), (50, 25), (20, 20), (18, 45)]
    sources = [
        SpriteSource(
            id=f"spr-{index}",
            content_key=f"content-{index}",
            base_path=Path("."),
            base_sha256=f"sha-{index}",
            emission_path=None,
            emission_sha256=None,
            output_scale=1.0,
            width=width,
            height=height,
        )
        for index, (width, height) in enumerate(dimensions)
    ]
    prepared = [
        PreparedSprite(
            source=source,
            base=Image.new("RGBA", (source.width, source.height)),
            emission=None,
            width=source.width,
            height=source.height,
            pivot_x=0.5,
            pivot_y=0.0,
        )
        for source in sources
    ]
    try:
        old_pages = _pack_shelves(sources, 128, 2)[1]
        plan = _pack_compact(sources, prepared, 128, 2, 1)
        reversed_plan = _pack_compact(
            list(reversed(sources)), list(reversed(prepared)), 128, 2, 1
        )
        assert len(plan.page_sizes) <= len(old_pages)
        assert sum(width * height for width, height in plan.page_sizes) < sum(
            width * height for width, height in old_pages
        )
        assert plan.page_sizes == reversed_plan.page_sizes
        assert plan.placements == reversed_plan.placements
        for sprite in prepared:
            placement = plan.placements[sprite.id]
            page_width, page_height = plan.page_sizes[placement.page]
            assert placement.x >= 1 and placement.y >= 1
            assert placement.x + sprite.width + 1 <= page_width
            assert placement.y + sprite.height + 1 <= page_height
        for index, left in enumerate(prepared):
            left_position = plan.placements[left.id]
            for right in prepared[index + 1 :]:
                right_position = plan.placements[right.id]
                if left_position.page != right_position.page:
                    continue
                separated = (
                    left_position.x + left.width + 1 + 2 <= right_position.x - 1
                    or right_position.x + right.width + 1 + 2 <= left_position.x - 1
                    or left_position.y + left.height + 1 + 2 <= right_position.y - 1
                    or right_position.y + right.height + 1 + 2 <= left_position.y - 1
                )
                assert separated
    finally:
        for sprite in prepared:
            sprite.base.close()


def test_joint_alpha_trim_preserves_anchor_and_emission_geometry(tmp_path: Path) -> None:
    base_path = tmp_path / "base.png"
    emission_path = tmp_path / "emission.png"
    Image.new("RGBA", (10, 8), (0, 0, 0, 0)).save(base_path)
    emission = Image.new("RGBA", (10, 8), (0, 0, 0, 0))
    emission.putpixel((0, 2), (255, 80, 20, 255))
    emission.save(emission_path)
    emission.close()
    source = SpriteSource(
        id="spr-union",
        content_key="union",
        base_path=base_path,
        base_sha256="base",
        emission_path=emission_path,
        emission_sha256="emission",
        output_scale=1.0,
        width=10,
        height=8,
    )
    prepared = _prepare_sprites([source], frame_padding=1)
    try:
        sprite = prepared[0]
        assert sprite.base.size == sprite.emission.size == (6, 7)
        assert sprite.pivot_x == pytest.approx(5 / 6)
        assert sprite.pivot_y == 0.0
        plan = _pack_compact([source], prepared, 64, 2, 1)
        base_pages, emission_pages = _encode_pages(
            prepared, plan.placements, plan.page_sizes, 1
        )
        assert len(base_pages) == len(emission_pages) == 1
        placement = plan.placements[source.id]
        with Image.open(BytesIO(emission_pages[0])) as page:
            assert page.getpixel((placement.x, placement.y + 1)) == (255, 80, 20)
            assert page.getpixel((placement.x - 1, placement.y + 1)) == (255, 80, 20)
    finally:
        for sprite in prepared:
            sprite.base.close()
            if sprite.emission is not None:
                sprite.emission.close()


def test_compact_packing_rejects_sprite_larger_than_page() -> None:
    source = SpriteSource(
        id="too-large",
        content_key="too-large",
        base_path=Path("."),
        base_sha256="sha",
        emission_path=None,
        emission_sha256=None,
        output_scale=1.0,
        width=127,
        height=8,
    )
    prepared = [
        PreparedSprite(
            source=source,
            base=Image.new("RGBA", (127, 8)),
            emission=None,
            width=127,
            height=8,
            pivot_x=0.5,
            pivot_y=0.0,
        )
    ]
    try:
        with pytest.raises(WorkspaceFormatError, match="超过图集"):
            _pack_compact([source], prepared, 128, 2, 1)
    finally:
        prepared[0].base.close()


def test_compact_packing_handles_long_strips_and_multiple_pages() -> None:
    dimensions = [(122, 4), (100, 100), (100, 100), (100, 100)]
    sources = [
        SpriteSource(
            id=f"shape-{index}",
            content_key=f"shape-{index}",
            base_path=Path("."),
            base_sha256=f"sha-{index}",
            emission_path=None,
            emission_sha256=None,
            output_scale=1.0,
            width=width,
            height=height,
        )
        for index, (width, height) in enumerate(dimensions)
    ]
    prepared = [
        PreparedSprite(
            source=source,
            base=Image.new("RGBA", (source.width, source.height)),
            emission=None,
            width=source.width,
            height=source.height,
            pivot_x=0.5,
            pivot_y=0.0,
        )
        for source in sources
    ]
    try:
        old_pages = _pack_shelves(sources, 128, 2)[1]
        plan = _pack_compact(sources, prepared, 128, 2, 1)
        assert 1 < len(plan.page_sizes) <= len(old_pages)
        assert all(width <= 128 and height <= 128 for width, height in plan.page_sizes)
        for left_index, left in enumerate(prepared):
            left_at = plan.placements[left.id]
            for right in prepared[left_index + 1 :]:
                right_at = plan.placements[right.id]
                if left_at.page != right_at.page:
                    continue
                assert (
                    left_at.x + left.width <= right_at.x
                    or right_at.x + right.width <= left_at.x
                    or left_at.y + left.height <= right_at.y
                    or right_at.y + right.height <= left_at.y
                )
    finally:
        for sprite in prepared:
            sprite.base.close()


def test_fully_transparent_sprite_trims_to_anchor_neighborhood(tmp_path: Path) -> None:
    path = tmp_path / "transparent.png"
    Image.new("RGBA", (10, 8), (0, 0, 0, 0)).save(path)
    source = SpriteSource(
        id="transparent",
        content_key="transparent",
        base_path=path,
        base_sha256="sha",
        emission_path=None,
        emission_sha256=None,
        output_scale=1.0,
        width=10,
        height=8,
    )
    prepared = _prepare_sprites([source], frame_padding=0)
    try:
        assert prepared[0].base.size == (1, 1)
        assert prepared[0].base.getpixel((0, 0)) == (0, 0, 0, 0)
        assert 0 <= prepared[0].pivot_x <= 1
        assert prepared[0].pivot_y == 0.0
    finally:
        prepared[0].base.close()


def test_action_opacity_immediately_attenuates_shadow_and_matches_export(
    tmp_path: Path,
) -> None:
    _, _, repository, character, _, action = _fixture(tmp_path)
    frames = copy.deepcopy(action["frameRefs"])
    for frame in frames:
        frame["transform"]["scale"] = {"x": 1.0, "y": 1.0}
        frame["transform"]["opacity"] = 1.0
    frames[1]["transform"]["opacity"] = 0.25
    action = repository.replace_action_frame_refs(
        action["id"],
        frames,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    domain = repository.workspace_domain()
    resolved = resolve_domain_action_shadows(
        repository.root,
        domain,
        next(item for item in domain["characters"] if item["id"] == character["id"]),
        action["frameRefs"],
        loop=False,
    )

    assert resolved[1]["widthPx"] == pytest.approx(resolved[0]["widthPx"])
    assert resolved[1]["depthPx"] == pytest.approx(resolved[0]["depthPx"])
    assert resolved[1]["positionPx"] == pytest.approx(resolved[0]["positionPx"])
    assert resolved[1]["alpha"] == pytest.approx(
        resolved[0]["alpha"] * 0.5,
        abs=1e-6,
    )

    exported = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    exported_frames = exported["manifest"]["animations"][0]["frames"]
    assert [item["shadow"]["alpha"] for item in exported_frames] == [
        item["alpha"] for item in resolved
    ]


def test_disabled_action_frames_are_preserved_in_workspace_but_omitted_from_delivery(
    tmp_path: Path,
) -> None:
    _, _, repository, character, _, action = _fixture(tmp_path)
    frames = action["frameRefs"]
    frames[1]["enabled"] = False
    repository.replace_action_frame_refs(
        action["id"],
        frames,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )

    saved = repository.get_domain_action(action["id"])
    assert len(saved["frameRefs"]) == 3
    assert saved["frameRefs"][1]["enabled"] is False

    result = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    animation = result["manifest"]["animations"][0]
    assert len(animation["frames"]) == 2
    assert animation["durationSeconds"] == pytest.approx(0.4)
    assert animation["frameRate"] == pytest.approx(5.0)
    assert result["manifest"]["deduplication"]["referencedFrames"] == 2
    assert result["manifest"]["sprites"][0]["outputScale"] == pytest.approx(1.0)
    generations = [
        path
        for path in (repository.root / "exports" / "domain" / character["id"]).iterdir()
        if path.is_dir() and not path.name.startswith(".stage-")
    ]
    assert [path.name for path in generations] == [result["sha256"]]


def test_delivery_timing_validation_rejects_total_and_average_rate_mismatch() -> None:
    manifest = {
        "animations": [{
            "id": "action-run",
            "displayName": "Run",
            "durationSeconds": 0.6,
            "frameRate": 5.0,
            "frames": [
                {"index": 0, "durationSeconds": 0.1},
                {"index": 1, "durationSeconds": 0.2},
                {"index": 2, "durationSeconds": 0.3},
            ],
        }],
    }
    validate_character_animation_timings(manifest)

    wrong_total = copy.deepcopy(manifest)
    wrong_total["animations"][0]["durationSeconds"] = 0.5
    with pytest.raises(ValueError, match="累计帧时长与总时长不一致"):
        validate_character_animation_timings(wrong_total)

    wrong_rate = copy.deepcopy(manifest)
    wrong_rate["animations"][0]["frameRate"] = 24.0
    with pytest.raises(ValueError, match="平均帧率与总时长不一致"):
        validate_character_animation_timings(wrong_rate)

    no_enabled_frames = copy.deepcopy(manifest)
    no_enabled_frames["animations"][0]["frames"] = []
    with pytest.raises(ValueError, match="没有启用帧"):
        validate_character_animation_timings(no_enabled_frames)


def test_export_resolves_shadow_inheritance_runtime_loop_and_action_texture_scale(tmp_path: Path) -> None:
    _, _, repository, character, _, action = _fixture(tmp_path)
    revision = repository.workspace_domain()["revisionId"]
    repository.update_domain_character_settings(character["id"], {
        "shadow": {"enabled": True, "color": "#204060", "baseOpacity": 0.55, "lightAngleDegrees": 145},
        "delivery": {"defaultActionId": action["id"], "actionSettings": {action["id"]: {"textureScale": 1.5, "runtimeLoop": False, "includeInExport": True}}},
    }, expected_revision_id=revision)
    current = repository.get_domain_action(action["id"])
    for frame in current["frameRefs"]:
        frame["transform"]["shadow"].update(enabled=None, color=None, opacity=None)
    repository.replace_action_frame_refs(action["id"], current["frameRefs"], expected_revision_id=repository.workspace_domain()["revisionId"])
    result = export_domain_character(repository, character["id"], expected_revision_id=repository.workspace_domain()["revisionId"], atlas_max_size=128)
    manifest = result["manifest"]
    assert manifest["animations"][0]["loop"] is False
    assert manifest["animations"][0]["outputScale"] == 1.5
    assert manifest["sprites"][0]["rect"]["width"] < 15
    assert 0 <= manifest["sprites"][0]["pivot"]["x"] <= 1
    assert manifest["animations"][0]["frames"][0]["transform"]["shadow"] == {"enabled": True, "color": "#204060", "opacity": 0.55, "offset": {"x": 2.0, "y": -1.0}, "scale": {"x": 1.2, "y": 0.8}}


def test_export_uses_unity_size_profile_and_omits_unselected_actions(
    tmp_path: Path,
) -> None:
    _, _, repository, character, variant, included = _fixture(tmp_path)
    excluded = repository.create_domain_action(
        character["id"],
        "Editor Only",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.replace_action_frame_refs(
        excluded["id"],
        [_frame_ref(variant, 0, 1.0)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.update_domain_character_settings(
        character["id"],
        {
            "calibration": {
                "sizeProfiles": [
                    {
                        "id": "unity-profile",
                        "name": "世界尺寸",
                        "unitMode": "unity",
                        "width": 5.12,
                        "height": 7.68,
                    }
                ],
                "activeSizeProfileId": "unity-profile",
            },
            "delivery": {
                "defaultActionId": included["id"],
                "actionSettings": {
                    included["id"]: {
                        "textureScale": 1.0,
                        "runtimeLoop": True,
                        "includeInExport": True,
                    },
                    excluded["id"]: {
                        "textureScale": 8.0,
                        "runtimeLoop": True,
                        "includeInExport": False,
                    },
                },
            },
        },
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    result = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    manifest = result["manifest"]
    assert [animation["id"] for animation in manifest["animations"]] == [included["id"]]
    assert manifest["deduplication"]["referencedFrames"] == 3
    assert manifest["character"]["designSize"]["sourceUnit"] == "unity"
    assert manifest["character"]["designSize"]["widthPixels"] == 512
    assert manifest["character"]["designSize"]["heightPixels"] == 768
    assert manifest["character"]["designSize"]["widthWorld"] == pytest.approx(5.12)
    assert manifest["character"]["designSize"]["heightWorld"] == pytest.approx(7.68)


def test_shared_sprite_uses_maximum_included_action_texture_scale(tmp_path: Path) -> None:
    _, _, repository, character, variant, first = _fixture(tmp_path)
    second = repository.create_domain_action(
        character["id"],
        "Large Texture",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.replace_action_frame_refs(
        second["id"],
        [_frame_ref(variant, 0, 8.0)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.update_domain_character_settings(
        character["id"],
        {"delivery": {
            "defaultActionId": first["id"],
            "actionSettings": {
                first["id"]: {"textureScale": 0.5, "runtimeLoop": True, "includeInExport": True},
                second["id"]: {"textureScale": 2.5, "runtimeLoop": True, "includeInExport": True},
            },
        }},
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    result = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    sprite = result["manifest"]["sprites"][0]
    assert sprite["outputScale"] == pytest.approx(2.5)
    assert sprite["rect"]["width"] < 25
    estimate = estimate_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    assert estimate["maximumOutput"]["width"] == 25
    assert result["manifest"]["animations"][1]["frames"][0]["transform"]["scale"] == {"x": 8.0, "y": 8.0}


def test_atlas_repair_requires_same_size_rgba_and_replaces_current_generation(tmp_path: Path) -> None:
    _, _, repository, character, _, _ = _fixture(tmp_path)
    first = export_domain_character(repository, character["id"], expected_revision_id=repository.workspace_domain()["revisionId"], atlas_max_size=128)
    with zipfile.ZipFile(repository.root / first["archivePath"]) as archive:
        with Image.open(BytesIO(archive.read("atlases/base/00.png"))) as page:
            page_size = page.size
    repair = tmp_path / "repair.png"; Image.new("RGBA", page_size, (255, 0, 0, 128)).save(repair)
    result = repair_domain_atlas_page(repository, character["id"], 0, repair, expected_revision_id=repository.workspace_domain()["revisionId"])
    assert result["sha256"] != first["sha256"]
    assert repository.workspace_domain()["characters"][0]["exportState"]["currentAtlas"]["sha256"] == result["sha256"]
    wrong = tmp_path / "wrong.png"; Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(wrong)
    with pytest.raises(WorkspaceFormatError, match="尺寸"):
        repair_domain_atlas_page(repository, character["id"], 0, wrong, expected_revision_id=repository.workspace_domain()["revisionId"])


def test_export_replaces_the_previous_generation_and_rejects_missing_reference(
    tmp_path: Path,
) -> None:
    _, _, repository, character, variant, action = _fixture(tmp_path)
    first = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    changed = [_frame_ref(variant, 0, 1.0, duration=0.5)]
    repository.replace_action_frame_refs(
        action["id"],
        changed,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    second = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    assert first["sha256"] != second["sha256"]
    generations = [
        path
        for path in (repository.root / "exports" / "domain" / character["id"]).iterdir()
        if path.is_dir() and not path.name.startswith(".stage-")
    ]
    assert [path.name for path in generations] == [second["sha256"]]

    malformed = copy.deepcopy(repository.workspace_domain())
    malformed["actions"][0]["frameRefs"][0]["frameId"] = "varf_missing"
    with pytest.raises(WorkspaceFormatError, match="不存在的处理帧"):
        _collect_sources(repository.root, malformed, malformed["characters"][0])


def test_replacing_a_tracked_export_does_not_poison_the_workspace_session(
    tmp_path: Path,
) -> None:
    _, _, repository, character, variant, action = _fixture(tmp_path)
    first = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    repository.validate(full_hash=False)

    repository.replace_action_frame_refs(
        action["id"],
        [_frame_ref(variant, 0, 1.0, duration=0.5)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    second = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )

    assert first["sha256"] != second["sha256"]
    assert not (repository.root / first["archivePath"]).exists()
    assert (repository.root / second["archivePath"]).is_file()
    renamed = repository.update_domain_character(
        character["id"],
        name="角色包替换后仍可写入",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    assert renamed["name"] == "角色包替换后仍可写入"


def test_hash_damage_and_disk_full_preserve_current_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, repository, character, variant, _ = _fixture(tmp_path)
    first = export_domain_character(
        repository,
        character["id"],
        expected_revision_id=repository.workspace_domain()["revisionId"],
        atlas_max_size=128,
    )
    original_state = copy.deepcopy(
        repository.workspace_domain()["characters"][0]["exportState"]
    )
    frame_path = repository.root / variant["frames"][0]["path"]
    frame_path.write_bytes(frame_path.read_bytes() + b"damage")
    with pytest.raises(WorkspaceFormatError, match="校验失败"):
        export_domain_character(
            repository,
            character["id"],
            expected_revision_id=repository.workspace_domain()["revisionId"],
            atlas_max_size=128,
        )
    assert repository.workspace_domain()["characters"][0]["exportState"] == original_state
    # Restore the exact file before simulating ENOSPC.
    frame_path.write_bytes(frame_path.read_bytes()[:-6])
    monkeypatch.setattr(
        "backend.app.domain_character_exporter.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=0),
    )
    with pytest.raises(WorkspaceFormatError, match="空间不足"):
        export_domain_character(
            repository,
            character["id"],
            expected_revision_id=repository.workspace_domain()["revisionId"],
            atlas_max_size=128,
        )
    current = repository.workspace_domain()["characters"][0]["exportState"]
    assert current == original_state
    assert (repository.root / first["archivePath"]).is_file()
    assert not list(
        (repository.root / "exports" / "domain" / character["id"]).glob(".stage-*")
    )


@pytest.mark.anyio
async def test_domain_export_api_and_download(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path / "runtime",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Export API")
    repository = app.state.workspace_session.require_repository()
    # Reuse a separately built workspace by constructing the minimal graph in
    # this app-owned repository.
    character = repository.create_domain_character("API Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"api-export")
    frame = fixture / "frame.png"
    Image.new("RGBA", (8, 8), (120, 60, 20, 255)).save(frame)
    logical = frame.relative_to(repository.root).as_posix()
    source = repository.create_material_source(
        character["id"],
        "Source",
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
    action = repository.create_domain_action(
        character["id"],
        "Idle",
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    repository.replace_action_frame_refs(
        action["id"],
        [_frame_ref(variant, 0, 1.0)],
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45030))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                f"/api/v4/domain/characters/{character['id']}/export",
                json={
                    "expectedRevisionId": repository.workspace_domain()["revisionId"],
                    "atlasMaxSize": 128,
                },
            )
            assert response.status_code == 200, response.text
            download = await client.get(
                f"/api/v4/domain/characters/{character['id']}/export/download"
            )
            assert download.status_code == 200
            assert download.content.startswith(b"PK")
    finally:
        app.state.workspace_session.shutdown()
