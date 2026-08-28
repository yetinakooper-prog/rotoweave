from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from backend.app.basic_material_processor import process_basic_material
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.workspace_session import WorkspaceSessionManager


def _source(repository, root: Path, character_id: str, name: str, images: list[np.ndarray]):
    video = root / "fixtures" / f"{name}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(f"video-{name}".encode())
    paths = []
    metadata = []
    for index, image in enumerate(images):
        path = root / "fixtures" / name / f"{index:06d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        assert cv2.imwrite(str(path), image)
        paths.append(path.relative_to(root).as_posix())
        metadata.append(
            {
                "linearPath": path.relative_to(root).as_posix(),
                "ptsUs": round(index / 24 * 1_000_000),
                "durationUs": round(1_000_000 / 24),
                "width": image.shape[1],
                "height": image.shape[0],
            }
        )
    return repository.create_material_source(
        character_id,
        name,
        video.relative_to(root).as_posix(),
        paths,
        metadata={
            "fps": 24.0,
            "durationSeconds": len(images) / 24,
            "frameCount": len(images),
            "width": images[0].shape[1],
            "height": images[0].shape[0],
            "color": {
                "transfer": "bt709",
                "primaries": "bt709",
                "matrix": "bt709",
                "range": "tv",
            },
            "warnings": [],
        },
        frame_metadata=metadata,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )


def _workspace(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "runtime", runtime_root=tmp_path)
    session = WorkspaceSessionManager(settings)
    session.create(tmp_path / "workspace", "Basic 4")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    return settings, session, repository, character


def test_basic_character_and_effect_publish_rgba_variants(
    tmp_path: Path,
) -> None:
    settings, session, repository, character = _workspace(tmp_path)
    magenta = np.full((32, 40, 3), (255, 0, 255), dtype=np.uint8)
    magenta[8:26, 12:30] = (0, 220, 220)
    character_source = _source(
        repository, repository.root, character["id"], "character", [magenta]
    )
    result = process_basic_material(
        repository,
        character_source["id"],
        Path(session.runtime_root) / "basic-character",
        {
            "quality": "basic",
            "material_type": "character",
            "ai_assist": False,
            "chroma": {
                "screen_samples": [
                    {"rgb": [255, 0, 255], "color_space": "srgb"}
                ],
                "key_mode": "preserve_subject_screen_color",
            },
        },
        settings,
        lambda *_: None,
        lambda: None,
        expected_revision_id=repository.workspace_domain()["revisionId"],
        frame_indexes=[0],
    )
    variant = repository.get_material_variant(result["variantId"])
    assert variant is not None and variant["kind"] == "basic"
    output = cv2.imread(
        str(repository.root / variant["frames"][0]["path"]),
        cv2.IMREAD_UNCHANGED,
    )
    assert output is not None and output.shape == (32, 40, 4)
    assert int(output[0, 0, 3]) < 10
    assert int(output[16, 20, 3]) > 200
    assert not (Path(session.runtime_root) / "basic-character").exists()

    effect = np.zeros((24, 24, 3), dtype=np.uint8)
    effect[8:16, 8:16] = (80, 160, 255)
    effect_source = _source(
        repository, repository.root, character["id"], "effect", [effect]
    )
    effect_result = process_basic_material(
        repository,
        effect_source["id"],
        Path(session.runtime_root) / "basic-effect",
        {"quality": "basic", "material_type": "effect", "ai_assist": False},
        settings,
        lambda *_: None,
        lambda: None,
        expected_revision_id=repository.workspace_domain()["revisionId"],
        frame_indexes=[0],
    )
    effect_variant = repository.get_material_variant(effect_result["variantId"])
    effect_output = cv2.imread(
        str(repository.root / effect_variant["frames"][0]["path"]),
        cv2.IMREAD_UNCHANGED,
    )
    assert int(effect_output[0, 0, 3]) == 0
    assert int(effect_output[12, 12, 3]) == 255


def test_basic_subset_publishes_only_selected_source_frames(tmp_path: Path) -> None:
    settings, session, repository, character = _workspace(tmp_path)
    images = [
        np.full((18, 20, 3), (0, 255, 0), dtype=np.uint8),
        np.full((18, 20, 3), (40, 80, 160), dtype=np.uint8),
        np.full((18, 20, 3), (0, 0, 0), dtype=np.uint8),
    ]
    source = _source(repository, repository.root, character["id"], "subset", images)
    result = process_basic_material(
        repository,
        source["id"],
        Path(session.runtime_root) / "basic-subset",
        {"quality": "basic", "material_type": "effect", "ai_assist": False},
        settings,
        lambda *_: None,
        lambda: None,
        expected_revision_id=repository.workspace_domain()["revisionId"],
        frame_indexes=[0, 2],
    )
    variant = repository.get_material_variant(result["variantId"])
    assert result["frameCount"] == 2
    assert [item["index"] for item in result["frames"]] == [0, 2]
    assert [item["sourceFrameId"] for item in variant["frames"]] == [
        source["frames"][0]["id"],
        source["frames"][2]["id"],
    ]


def test_basic_job_idempotency_key_isolated_by_selected_range(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", runtime_root=tmp_path)
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Basic Idempotency")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    image = np.full((12, 12, 3), (0, 255, 0), dtype=np.uint8)
    source = _source(
        repository,
        repository.root,
        character["id"],
        "two-ranges",
        [image, image.copy()],
    )
    revision = repository.workspace_domain()["revisionId"]
    settings_payload = {"quality": "basic", "material_type": "effect", "ai_assist": False}
    first = app.state.jobs.create_material_basic(
        source["id"], settings_payload, expected_revision_id=revision, frame_indexes=[0]
    )
    replay = app.state.jobs.create_material_basic(
        source["id"], settings_payload, expected_revision_id=revision, frame_indexes=[0]
    )
    other = app.state.jobs.create_material_basic(
        source["id"], settings_payload, expected_revision_id=revision, frame_indexes=[1]
    )
    assert replay["id"] == first["id"]
    assert other["id"] != first["id"]
    assert other["cache_key"] != first["cache_key"]
    app.state.runtime.shutdown()


def test_basic_oom_and_model_unavailable_never_publish_half_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, repository, character = _workspace(tmp_path)
    image = np.full((16, 16, 3), (0, 255, 0), dtype=np.uint8)
    source = _source(repository, repository.root, character["id"], "risk", [image])

    original_chroma = __import__(
        "backend.app.basic_material_processor", fromlist=["chroma_rgba"]
    ).chroma_rgba

    def risky(*args, **kwargs):
        rgba, alpha, qc = original_chroma(*args, **kwargs)
        return rgba, alpha, {**qc, "empty_mask": True}

    monkeypatch.setattr("backend.app.basic_material_processor.chroma_rgba", risky)
    monkeypatch.setattr(
        "backend.app.basic_material_processor.preflight_birefnet",
        lambda *_: (_ for _ in ()).throw(RuntimeError("CUDA out of memory")),
    )
    with pytest.raises(RuntimeError, match="gpu_out_of_memory"):
        process_basic_material(
            repository,
            source["id"],
            Path(session.runtime_root) / "basic-oom",
            {"quality": "basic", "material_type": "character"},
            settings,
            lambda *_: None,
            lambda: None,
            expected_revision_id=repository.workspace_domain()["revisionId"],
            frame_indexes=[0],
        )
    assert repository.workspace_domain()["materialVariants"] == []
    assert not (Path(session.runtime_root) / "basic-oom").exists()

    monkeypatch.setattr(
        "backend.app.basic_material_processor.preflight_birefnet",
        lambda *_: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )
    degraded = process_basic_material(
        repository,
        source["id"],
        Path(session.runtime_root) / "basic-degraded",
        {"quality": "basic", "material_type": "character"},
        settings,
        lambda *_: None,
        lambda: None,
        expected_revision_id=repository.workspace_domain()["revisionId"],
        frame_indexes=[0],
    )
    assert degraded["warnings"] and "model-unavailable" in degraded["warnings"][0]
    assert repository.get_material_variant(degraded["variantId"]) is not None


async def _wait_job(client: httpx.AsyncClient, job_id: str, terminal: set[str]):
    for _ in range(200):
        jobs = (await client.get("/api/v4/jobs")).json()
        job = next(item for item in jobs if item["id"] == job_id)
        if job["status"] in terminal:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {terminal}")


@pytest.mark.anyio
async def test_basic_job_rejects_empty_duplicate_and_out_of_range_selection(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Basic Selection")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    image = np.full((12, 12, 3), (0, 255, 0), dtype=np.uint8)
    source = _source(
        repository, repository.root, character["id"], "selection", [image, image]
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45001))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            base = {
                "expectedRevisionId": repository.workspace_domain()["revisionId"],
                "settings": {"quality": "basic", "material_type": "effect", "ai_assist": False},
            }
            missing = await client.post(
                f"/api/v4/material-sources/{source['id']}/basic-jobs",
                json=base,
            )
            assert missing.status_code == 422, missing.text
            missing_remote = await client.post(
                f"/api/v4/material-sources/{source['id']}/remote-jobs",
                json={
                    "expectedRevisionId": base["expectedRevisionId"],
                    "quality": "high",
                    "settings": {"material_type": "character"},
                },
            )
            assert missing_remote.status_code == 422, missing_remote.text
            for indexes in ([], [0, 0], [1, 0], [2]):
                response = await client.post(
                    f"/api/v4/material-sources/{source['id']}/basic-jobs",
                    json={**base, "frameIndexes": indexes},
                )
                assert response.status_code == 422, response.text
            retired_enum = await client.post(
                f"/api/v4/material-sources/{source['id']}/basic-jobs",
                json={
                    **base,
                    "frameIndexes": [0],
                    "settings": {
                        **base["settings"],
                        "chroma": {"key_mode": "preserve_subject_green"},
                    },
                },
            )
            assert retired_enum.status_code == 422, retired_enum.text
            assert repository.get_material_source(source["id"])["variantIds"] == []
    finally:
        app.state.runtime.shutdown()


@pytest.mark.anyio
async def test_basic_job_api_is_idempotent_and_cancel_has_one_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_root=tmp_path / "data",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Basic Jobs")
    repository = app.state.workspace_session.require_repository()
    character = repository.create_domain_character("Hero")
    image = np.full((20, 20, 3), (255, 0, 255), dtype=np.uint8)
    image[5:15, 5:15] = (0, 200, 200)
    source = _source(repository, repository.root, character["id"], "job", [image])
    app.state.jobs.start()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45001))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            revision = repository.workspace_domain()["revisionId"]
            request = {
                "expectedRevisionId": revision,
                "frameIndexes": [0],
                "settings": {
                    "quality": "basic",
                    "material_type": "character",
                    "ai_assist": False,
                    "chroma": {
                        "screen_samples": [
                            {"rgb": [255, 0, 255], "color_space": "srgb"}
                        ]
                    },
                },
            }
            created = await client.post(
                f"/api/v4/material-sources/{source['id']}/basic-jobs",
                json=request,
            )
            assert created.status_code == 202, created.text
            completed = await _wait_job(
                client, created.json()["id"], {"completed", "failed"}
            )
            assert completed["status"] == "completed", completed
            replay = await client.post(
                f"/api/v4/material-sources/{source['id']}/basic-jobs",
                json=request,
            )
            assert replay.status_code == 202
            assert replay.json()["id"] == completed["id"]

            second_source = _source(
                repository,
                repository.root,
                character["id"],
                "cancel",
                [image],
            )
            started = threading.Event()

            def wait_for_cancel(
                _repository,
                _source_id,
                _output_root,
                _settings,
                _runtime_settings,
                _report,
                check_control,
                *,
                expected_revision_id,
                frame_indexes,
            ):
                del expected_revision_id, frame_indexes
                started.set()
                while True:
                    check_control()
                    time.sleep(0.005)

            monkeypatch.setattr("backend.app.jobs.process_basic_material", wait_for_cancel)
            cancel_request = {
                "expectedRevisionId": repository.workspace_domain()["revisionId"],
                "frameIndexes": [0],
                "settings": {
                    "quality": "basic",
                    "material_type": "effect",
                    "ai_assist": False,
                },
            }
            active = await client.post(
                f"/api/v4/material-sources/{second_source['id']}/basic-jobs",
                json=cancel_request,
            )
            assert active.status_code == 202
            assert await asyncio.to_thread(started.wait, 2)
            cancelled = await client.post(
                f"/api/v4/jobs/{active.json()['id']}/cancel"
            )
            assert cancelled.status_code == 200
            terminal = await _wait_job(
                client, active.json()["id"], {"cancelled", "completed", "failed"}
            )
            assert terminal["status"] == "cancelled"
            assert repository.get_material_source(second_source["id"])["variantIds"] == []
    finally:
        app.state.jobs.stop()
        app.state.runtime.shutdown()
