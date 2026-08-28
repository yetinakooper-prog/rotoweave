from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from backend.app.config import Settings
from backend.app.remote_matting_client import (
    ARCHIVE_SHA256_HEADER,
    DownloadedRemoteResult,
    RemoteIntegrityError,
    RemoteMattingClient,
    RemoteMattingConfig,
    RemoteResponseError,
    canonical_sha256,
    inspect_remote_result,
    prepare_remote_submission,
    publish_remote_result,
    result_payload_sha256,
)
from backend.app.remote_protocol import (
    RemoteOutputFrame,
    RemoteQuality,
    RemoteResultManifest,
)
from backend.app.workspace_session import WorkspaceSessionManager


def _workspace(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "runtime", runtime_root=tmp_path)
    session = WorkspaceSessionManager(settings)
    session.create(tmp_path / "workspace", "Remote 4")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    fixture = repository.root / "_fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"remote-source")
    frame_paths = []
    frame_metadata = []
    for index in range(2):
        image = np.full((12, 16, 3), (0, 255, 0), dtype=np.uint8)
        image[3:10, 4 + index : 12 + index] = (30, 140, 230)
        path = fixture / f"{index:06d}.png"
        assert cv2.imwrite(str(path), image)
        logical = path.relative_to(repository.root).as_posix()
        frame_paths.append(logical)
        frame_metadata.append(
            {
                "linearPath": logical,
                "ptsUs": index * 41_667,
                "durationUs": 41_667,
                "width": 16,
                "height": 12,
            }
        )
    source = repository.create_material_source(
        character["id"],
        "Remote Source",
        video.relative_to(repository.root).as_posix(),
        frame_paths,
        metadata={
            "fps": 24.0,
            "durationSeconds": 2 / 24,
            "frameCount": 2,
            "width": 16,
            "height": 12,
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
    return repository, source


def _status(job_id: str, state: str, progress: float) -> dict[str, object]:
    return {
        "protocolVersion": 1,
        "jobId": job_id,
        "state": state,
        "progress": progress,
        "stage": state,
        "error": None,
    }


def _png(rgba: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", rgba)
    assert ok
    return encoded.tobytes()


def _result_archive(
    path: Path,
    source: dict,
    *,
    corrupt: bool = False,
    frame_indexes: list[int] | None = None,
) -> str:
    members: dict[str, bytes] = {}
    records = []
    selected_frames = [
        source["frames"][index]
        for index in (frame_indexes if frame_indexes is not None else range(len(source["frames"])))
    ]
    for index, source_frame in enumerate(selected_frames):
        rgba = np.zeros((12, 16, 4), dtype=np.uint8)
        rgba[2:10, 3:13] = (30, 140, 230, 255)
        rgba_bytes = _png(rgba)
        emission = np.zeros((12, 16, 4), dtype=np.uint8)
        emission[4:8, 6:10] = (60, 120, 255, 255)
        emission_bytes = _png(emission)
        rgba_path = f"rgba/{index:06d}.png"
        emission_path = f"emission/{index:06d}.png"
        members[rgba_path] = rgba_bytes
        members[emission_path] = emission_bytes
        records.append(
            RemoteOutputFrame(
                sourceFrameId=source_frame["id"],
                ordinal=index,
                width=16,
                height=12,
                rgbaPath=rgba_path,
                rgbaSha256=hashlib.sha256(rgba_bytes).hexdigest(),
                emissionPath=emission_path,
                emissionSha256=hashlib.sha256(emission_bytes).hexdigest(),
            )
        )
    manifest = RemoteResultManifest(
        protocolVersion=1,
        jobId="remote-job",
        materialId=source["id"],
        materialSha256=source["video"]["sha256"],
        quality=RemoteQuality.HIGH,
        frameCount=len(records),
        frameMappingSha256=canonical_sha256(
            [item.model_dump(mode="json") for item in records]
        ),
        archiveSha256="0" * 64,
        frames=records,
        model={"id": "sam2matting-bplus", "sha256": "a" * 64},
        settings={"quality": "high", "subject": "character"},
    )
    content_hash = result_payload_sha256(manifest, members.__getitem__)
    manifest = manifest.model_copy(update={"archiveSha256": content_hash})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("result.json", manifest.model_dump_json())
        for name, content in members.items():
            archive.writestr(name, b"corrupt" if corrupt and name == "rgba/000001.png" else content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_remote_config_requires_http_and_private_fixed_ipv4() -> None:
    assert RemoteMattingConfig("http://192.168.1.40:8443").api_url.endswith("/api/matting/v1")
    with pytest.raises(ValueError, match="HTTP URL"):
        RemoteMattingConfig("https://192.168.1.40:8443")
    with pytest.raises(ValueError, match="固定 IPv4"):
        RemoteMattingConfig("http://server:8443")
    with pytest.raises(ValueError, match="私网 IPv4"):
        RemoteMattingConfig("http://8.8.8.8:8443")


def test_remote_submission_and_publish_preserve_selected_subset(tmp_path: Path) -> None:
    repository, source = _workspace(tmp_path)
    first = prepare_remote_submission(
        repository,
        source["id"],
        "high",
        {"subject": "character"},
        tmp_path / "selected.zip",
        frame_indexes=[1],
    )
    other = prepare_remote_submission(
        repository,
        source["id"],
        "high",
        {"subject": "character"},
        tmp_path / "other.zip",
        frame_indexes=[0],
    )
    assert first.submission.frameCount == 1
    assert first.submission.frames[0].frameId == source["frames"][1]["id"]
    assert first.idempotency_key != other.idempotency_key

    archive = tmp_path / "selected-result.zip"
    transport_hash = _result_archive(archive, source, frame_indexes=[1])
    downloaded = DownloadedRemoteResult(
        archive_path=archive,
        transport_sha256=transport_hash,
        manifest=inspect_remote_result(archive),
    )
    variant = publish_remote_result(
        repository,
        source["id"],
        downloaded,
        tmp_path / "publish-selected",
        expected_revision_id=repository.workspace_domain()["revisionId"],
        expected_source_frame_ids=[source["frames"][1]["id"]],
    )
    assert [item["sourceFrameId"] for item in variant["frames"]] == [
        source["frames"][1]["id"]
    ]
    before = repository.workspace_domain()
    with pytest.raises(RemoteIntegrityError, match="本次选择"):
        publish_remote_result(
            repository,
            source["id"],
            downloaded,
            tmp_path / "publish-wrong-mapping",
            expected_revision_id=before["revisionId"],
            expected_source_frame_ids=[source["frames"][0]["id"]],
        )
    assert repository.workspace_domain() == before
    assert not (tmp_path / "publish-wrong-mapping").exists()


@pytest.mark.anyio
async def test_submit_is_hashed_idempotent_and_retries_server_restart(
    tmp_path: Path,
) -> None:
    repository, source = _workspace(tmp_path)
    prepared = prepare_remote_submission(
        repository,
        source["id"],
        "high",
        {"subject": "character"},
        tmp_path / "runtime" / "upload.zip",
        frame_indexes=[0, 1],
    )
    assert prepared.submission.frameCount == 2
    assert prepared.submission.framesManifestSha256 == canonical_sha256(
        [item.model_dump(mode="json") for item in prepared.submission.frames]
    )
    assert prepared.submission.archiveSha256 == hashlib.sha256(
        prepared.archive_path.read_bytes()
    ).hexdigest()
    calls = 0
    keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        keys.append(request.headers["Idempotency-Key"])
        assert "Authorization" not in request.headers
        assert request.headers["X-RotoWeave-Protocol-Version"] == "1"
        body = await request.aread()
        assert source["id"].encode() in body
        if calls == 1:
            return httpx.Response(
                503,
                json={
                    "protocolVersion": 1,
                    "code": "internal_error",
                    "message": "restarting",
                    "retryable": True,
                    "detail": None,
                },
            )
        return httpx.Response(202, json=_status("remote-job", "queued", 0.0))

    config = RemoteMattingConfig("http://192.168.1.40:8443")
    async with RemoteMattingClient(
        config, transport=httpx.MockTransport(handler)
    ) as client:
        status = await client.submit(prepared)
    assert status.jobId == "remote-job"
    assert calls == 2
    assert keys == [prepared.idempotency_key, prepared.idempotency_key]


@pytest.mark.anyio
async def test_sse_reconnect_cancel_and_protocol_error_are_explicit() -> None:
    event_calls = 0
    status_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal event_calls, status_calls
        if request.url.path.endswith("/events"):
            event_calls += 1
            if event_calls == 1:
                body = "id: 0\ndata: " + json.dumps(
                    {
                        "protocolVersion": 1,
                        "jobId": "remote-job",
                        "sequence": 0,
                        "state": "running",
                        "progress": 0.4,
                        "stage": "matting",
                        "message": None,
                    }
                ) + "\n\n"
            else:
                assert request.headers["Last-Event-ID"] == "0"
                body = "id: 1\ndata: " + json.dumps(
                    {
                        "protocolVersion": 1,
                        "jobId": "remote-job",
                        "sequence": 1,
                        "state": "completed",
                        "progress": 1.0,
                        "stage": "completed",
                        "message": None,
                    }
                ) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json=_status("remote-job", "cancelled", 0.4))
        status_calls += 1
        if status_calls == 1:
            raise httpx.ReadTimeout("request timed out", request=request)
        if status_calls == 2:
            raise httpx.ConnectError("server restarted", request=request)
        return httpx.Response(
            409,
            json={
                "protocolVersion": 1,
                "code": "incompatible_protocol",
                "message": "protocol mismatch",
                "retryable": False,
                "detail": {"supported": 1},
            },
        )

    config = RemoteMattingConfig("http://192.168.1.40", max_retries=3)
    async with RemoteMattingClient(
        config, transport=httpx.MockTransport(handler)
    ) as client:
        events = [event async for event in client.events("remote-job")]
        assert [item.sequence for item in events] == [0, 1]
        cancelled = await client.cancel("remote-job")
        assert cancelled.state.value == "cancelled"
        with pytest.raises(RemoteResponseError) as captured:
            await client.status("remote-job")
    assert captured.value.error.code.value == "incompatible_protocol"
    assert status_calls == 3


@pytest.mark.anyio
async def test_result_hash_validation_and_publish_preserve_optional_layers(
    tmp_path: Path,
) -> None:
    repository, source = _workspace(tmp_path)
    server_archive = tmp_path / "server-result.zip"
    transport_hash = _result_archive(server_archive, source)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("download interrupted", request=request)
        return httpx.Response(
            200,
            headers={ARCHIVE_SHA256_HEADER: transport_hash},
            content=server_archive.read_bytes(),
        )

    config = RemoteMattingConfig("http://192.168.1.40")
    async with RemoteMattingClient(
        config, transport=httpx.MockTransport(handler)
    ) as client:
        downloaded = await client.download_result(
            "remote-job", tmp_path / "runtime" / "result.zip"
        )
    revision = repository.workspace_domain()["revisionId"]
    variant = publish_remote_result(
        repository,
        source["id"],
        downloaded,
        tmp_path / "runtime" / "publish",
        expected_revision_id=revision,
    )
    assert variant["kind"] == "high"
    assert [item["sourceFrameId"] for item in variant["frames"]] == [
        item["id"] for item in source["frames"]
    ]
    assert all(item.get("emission", {}).get("sha256") for item in variant["frames"])
    assert not (tmp_path / "runtime" / "publish").exists()
    assert calls == 2


@pytest.mark.anyio
async def test_corrupt_result_never_publishes_or_leaves_partial_download(
    tmp_path: Path,
) -> None:
    repository, source = _workspace(tmp_path)
    server_archive = tmp_path / "corrupt.zip"
    transport_hash = _result_archive(server_archive, source, corrupt=True)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={ARCHIVE_SHA256_HEADER: transport_hash},
            content=server_archive.read_bytes(),
        )

    destination = tmp_path / "runtime" / "corrupt-result.zip"
    config = RemoteMattingConfig("http://192.168.1.40")
    async with RemoteMattingClient(
        config, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RemoteIntegrityError, match="图层哈希"):
            await client.download_result("remote-job", destination)
    assert not destination.exists()
    assert not destination.with_suffix(".zip.part").exists()
    assert repository.workspace_domain()["materialVariants"] == []
