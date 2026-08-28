from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from backend.app.config import Settings
from backend.app.remote_matting_client import (
    RemoteMattingClient,
    RemoteMattingConfig,
    RemoteResponseError,
    canonical_sha256,
    prepare_remote_submission,
    result_payload_sha256,
)
from backend.app.remote_protocol import RemoteOutputFrame, RemoteResultManifest
from backend.app.workspace_session import WorkspaceSessionManager
from server.api import create_admin_app, create_remote_app
from server.config import RemoteServerSettings
from server.processor import ProcessingCancelled, RemoteProcessingError
from server.service import RemoteService


@pytest.mark.anyio
async def test_job_rejects_unavailable_profile_before_enqueue_with_structured_503(
    tmp_path: Path,
) -> None:
    repository, source = _source(tmp_path)
    processor = FakeCudaProcessor()
    processor.preflight_profile = lambda _profile: None  # type: ignore[attr-defined]
    settings, service = _server(tmp_path, processor)
    app = create_remote_app(settings, service=service, manage_lifecycle=False)
    prepared = prepare_remote_submission(
        repository,
        source["id"],
        "high",
        {"material_type": "character"},
        tmp_path / "client-state" / "profile-unavailable.zip",
        frame_indexes=[0],
    )
    transport = httpx.ASGITransport(app=app, client=("10.0.0.20", 50100))
    async with RemoteMattingClient(
        RemoteMattingConfig("http://192.168.1.40", max_retries=0),
        transport=transport,
    ) as client:
        with pytest.raises(RemoteResponseError) as captured:
            await client.submit(prepared)
    error = captured.value.error
    assert captured.value.status_code == 503
    assert error.code == "model_unavailable"
    assert error.retryable is False
    assert error.detail == {
        "reason": "profile_unavailable",
        "profile": "high",
        "warningCodes": ["profile_receipt_missing"],
        "recommendedActions": [
            "在模型中心执行 Verify → 分档 Self-test → Partial Activate。"
        ],
    }
    assert service.repository.stats()["states"].get("queued", 0) == 0


def _source(tmp_path: Path):
    local = Settings(data_root=tmp_path / "client-state", runtime_root=tmp_path)
    session = WorkspaceSessionManager(local)
    session.create(tmp_path / "workspace", "Remote Server Test")
    repository = session.require_repository()
    character = repository.create_domain_character("Hero")
    fixture = repository.root / "fixtures"
    fixture.mkdir()
    video = fixture / "source.mp4"
    video.write_bytes(b"remote-server-source")
    paths = []
    metadata = []
    for index in range(2):
        image = np.full((12, 16, 3), (0, 255, 0), dtype=np.uint8)
        image[2:10, 4:12] = (20 + index, 120, 230)
        path = fixture / f"{index:06d}.png"
        assert cv2.imwrite(str(path), image)
        logical = path.relative_to(repository.root).as_posix()
        paths.append(logical)
        metadata.append(
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
        "Server Source",
        video.relative_to(repository.root).as_posix(),
        paths,
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
        frame_metadata=metadata,
        expected_revision_id=repository.workspace_domain()["revisionId"],
    )
    return repository, source


def _png() -> bytes:
    rgba = np.zeros((12, 16, 4), dtype=np.uint8)
    rgba[2:10, 4:12] = (20, 120, 230, 255)
    ok, encoded = cv2.imencode(".png", rgba)
    assert ok
    return encoded.tobytes()


class FakeCudaProcessor:
    def __init__(self, *, delay: float = 0.01):
        self.delay = delay
        self.restart_count = 0
        self.warmup_count = 0
        self.closed = False
        self.active = 0
        self.max_active = 0
        self.started = threading.Event()
        self._guard = threading.Lock()

    def warmup(self):
        self.warmup_count += 1
        return {
            "hardware": {"selectedDevice": {"uuid": "GPU-test", "name": "NVIDIA Test GPU"}},
            "modelConfiguration": {
                "state": "ready",
                "configurationDigest": "a" * 64,
                "verifiedFileCount": 5,
            },
        }

    def process(self, job_id, submission, _input_archive, job_root, progress, check_control):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            progress(0.2, "worker", "fake worker running")
            if submission.settings.get("force_oom"):
                raise RemoteProcessingError(
                    "gpu_out_of_memory", "synthetic CUDA out of memory", retryable=True
                )
            deadline = time.monotonic() + self.delay
            while time.monotonic() < deadline:
                check_control()
                time.sleep(0.002)
            content = _png()
            members = {
                f"rgba/{frame.ordinal:06d}.png": content for frame in submission.frames
            }
            frames = [
                RemoteOutputFrame(
                    sourceFrameId=frame.frameId,
                    ordinal=frame.ordinal,
                    width=frame.width,
                    height=frame.height,
                    rgbaPath=f"rgba/{frame.ordinal:06d}.png",
                    rgbaSha256=hashlib.sha256(content).hexdigest(),
                )
                for frame in submission.frames
            ]
            manifest = RemoteResultManifest(
                protocolVersion=1,
                jobId=job_id,
                materialId=submission.materialId,
                materialSha256=submission.materialSha256,
                quality=submission.quality,
                frameCount=len(frames),
                frameMappingSha256=canonical_sha256(
                    [item.model_dump(mode="json") for item in frames]
                ),
                archiveSha256="0" * 64,
                frames=frames,
                model={"id": "fake-cuda", "sha256": "a" * 64},
                settings=submission.settings,
            )
            manifest = manifest.model_copy(
                update={"archiveSha256": result_payload_sha256(manifest, members.__getitem__)}
            )
            result = job_root / "result.zip"
            with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("result.json", manifest.model_dump_json())
                for name, payload in members.items():
                    archive.writestr(name, payload)
            progress(0.99, "ready", "fake result ready")
            return result, {"transportSha256": hashlib.sha256(result.read_bytes()).hexdigest()}
        finally:
            with self._guard:
                self.active -= 1

    def restart(self):
        self.restart_count += 1

    def close(self):
        self.closed = True


def _server(tmp_path: Path, processor: FakeCudaProcessor, *, ttl_hours: float = 24):
    settings = RemoteServerSettings(
        data_root=tmp_path / "server",
        ttl_hours=ttl_hours,
    )
    service = RemoteService(settings, processor=processor)
    return settings, service


async def _terminal(client: RemoteMattingClient, job_id: str):
    for _ in range(300):
        status = await client.status(job_id)
        if status.state.value in {"completed", "failed", "cancelled"}:
            return status
        await __import__("asyncio").sleep(0.01)
    raise AssertionError("remote job did not finish")


@pytest.mark.anyio
async def test_remote_api_full_contract_idempotency_sse_and_result(tmp_path: Path) -> None:
    repository, source = _source(tmp_path)
    processor = FakeCudaProcessor()
    settings, service = _server(tmp_path, processor)
    service.start()
    app = create_remote_app(settings, service=service, manage_lifecycle=False)
    prepared = prepare_remote_submission(
        repository,
        source["id"],
        "high",
        {"material_type": "character"},
        tmp_path / "client-state" / "upload.zip",
        frame_indexes=[0],
    )
    transport = httpx.ASGITransport(app=app, client=("10.0.0.20", 50100))
    try:
        async with RemoteMattingClient(
            RemoteMattingConfig("http://192.168.1.40"), transport=transport
        ) as client:
            first = await client.submit(prepared)
            replay = await client.submit(prepared)
            assert replay.jobId == first.jobId
            events = [event async for event in client.events(first.jobId)]
            assert events[-1].state.value == "completed"
            status = await client.status(first.jobId)
            assert status.state.value == "completed"
            downloaded = await client.download_result(
                first.jobId, tmp_path / "client-state" / "download.zip"
            )
            assert downloaded.manifest.materialSha256 == source["video"]["sha256"]
            assert downloaded.manifest.quality.value == "high"
        assert processor.max_active == 1
        assert service.repository.stats()["states"] == {"completed": 1}
    finally:
        service.stop()


@pytest.mark.anyio
async def test_remote_api_accepts_no_auth_http_and_rejects_protocol_mismatch(tmp_path: Path) -> None:
    processor = FakeCudaProcessor()
    settings, service = _server(tmp_path, processor)
    app = create_remote_app(settings, service=service, manage_lifecycle=False)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.20", 50100))
    async with httpx.AsyncClient(transport=transport, base_url="http://192.168.1.40") as client:
        anonymous = await client.get(
            "/api/matting/v1/jobs/missing", headers={"X-RotoWeave-Protocol-Version": "1"}
        )
        assert anonymous.status_code == 404
        legacy = await client.get(
            "/api/matting/v1/jobs/missing", headers={"X-AIFrame-Protocol-Version": "1"}
        )
        assert legacy.status_code == 404
        conflict = await client.get(
            "/api/matting/v1/jobs/missing",
            headers={
                "X-RotoWeave-Protocol-Version": "1",
                "X-AIFrame-Protocol-Version": "2",
            },
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == "identity_conflict"
        incompatible = await client.get(
            "/api/matting/v1/jobs/missing",
            headers={"X-RotoWeave-Protocol-Version": "2"},
        )
        assert incompatible.status_code == 409
        assert incompatible.json()["code"] == "incompatible_protocol"


@pytest.mark.anyio
async def test_remote_connection_status_needs_no_auth_and_leaks_no_queue_or_model(
    tmp_path: Path,
) -> None:
    processor = FakeCudaProcessor()
    settings, service = _server(tmp_path, processor)
    service.start()
    app = create_remote_app(settings, service=service, manage_lifecycle=False)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.20", 50100))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://192.168.1.40") as client:
            anonymous = await client.get(
                "/api/matting/v1/status",
                headers={"X-RotoWeave-Protocol-Version": "1"},
            )
            assert anonymous.status_code == 200
        async with RemoteMattingClient(
            RemoteMattingConfig("http://192.168.1.40"), transport=transport
        ) as client:
            for _ in range(100):
                status = await client.probe()
                if status.ready:
                    break
                await __import__("asyncio").sleep(0.01)
        payload = status.model_dump(mode="json")
        assert payload["protocolVersion"] == 1
        assert payload["ready"] is True
        assert payload["ownership"] == "short-lived-remote-jobs-only"
        assert "queue" not in payload
        assert "models" not in payload
        assert "paths" not in payload
        assert service.repository.stats()["states"] == {}
    finally:
        service.stop()


@pytest.mark.anyio
async def test_queue_is_serial_cancel_is_terminal_and_oom_restarts_worker(tmp_path: Path) -> None:
    repository, source = _source(tmp_path)
    processor = FakeCudaProcessor(delay=0.08)
    settings, service = _server(tmp_path, processor)
    service.start()
    app = create_remote_app(settings, service=service, manage_lifecycle=False)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.20", 50100))
    try:
        async with RemoteMattingClient(
            RemoteMattingConfig("http://192.168.1.40"), transport=transport
        ) as client:
            first_prepared = prepare_remote_submission(
                repository, source["id"], "high", {"batch": 1}, tmp_path / "client-state" / "one.zip", frame_indexes=[0]
            )
            second_prepared = prepare_remote_submission(
                repository, source["id"], "high", {"batch": 2}, tmp_path / "client-state" / "two.zip", frame_indexes=[0]
            )
            first = await client.submit(first_prepared)
            second = await client.submit(second_prepared)
            assert (await _terminal(client, first.jobId)).state.value == "completed"
            assert (await _terminal(client, second.jobId)).state.value == "completed"
            assert processor.max_active == 1

            processor.started.clear()
            cancel_prepared = prepare_remote_submission(
                repository, source["id"], "high", {"cancel": True}, tmp_path / "client-state" / "cancel.zip", frame_indexes=[0]
            )
            active = await client.submit(cancel_prepared)
            assert await __import__("asyncio").to_thread(processor.started.wait, 1)
            cancelled = await client.cancel(active.jobId)
            assert cancelled.state.value == "cancelled"
            assert (await _terminal(client, active.jobId)).state.value == "cancelled"

            # A current configuration can expose only the profiles that passed self-test.
            # Ultra becomes submit-ready after a dual Profile configuration is
            # verified, self-tested and activated through admin v2.
            oom_prepared = prepare_remote_submission(
                repository, source["id"], "high", {"force_oom": True}, tmp_path / "client-state" / "oom.zip", frame_indexes=[0]
            )
            oom = await client.submit(oom_prepared)
            failed = await _terminal(client, oom.jobId)
            assert failed.state.value == "failed"
            assert failed.error and failed.error.code.value == "gpu_out_of_memory"
            assert failed.error.retryable is True
            assert processor.restart_count >= 1
    finally:
        service.stop()


@pytest.mark.anyio
async def test_restart_recovery_ttl_admin_and_server_ownership_boundary(tmp_path: Path) -> None:
    repository, source = _source(tmp_path)
    first_processor = FakeCudaProcessor()
    settings, first_service = _server(tmp_path, first_processor)
    prepared = prepare_remote_submission(
        repository, source["id"], "high", {"recovery": True}, tmp_path / "client-state" / "recover.zip", frame_indexes=[0]
    )
    job, _ = first_service.submit(
        prepared.submission, prepared.archive_path, prepared.idempotency_key
    )
    claimed = first_service.repository.claim_next()
    assert claimed and claimed["state"] == "running"

    recovered_processor = FakeCudaProcessor()
    recovered = RemoteService(settings, processor=recovered_processor)
    recovered.start()
    try:
        for _ in range(300):
            status = recovered.status(str(job["id"]))
            if status and status.state.value == "completed":
                break
            await __import__("asyncio").sleep(0.01)
        else:
            raise AssertionError("recovered job did not complete")
        record = recovered.repository.get(str(job["id"]))
        assert record and record["attempt"] == 1

        with recovered.repository.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (job["id"],),
            )
        assert recovered.cleanup_expired()["removed"] == 1
        assert recovered.repository.get(str(job["id"])) is None

        admin = create_admin_app(recovered)
        local_transport = httpx.ASGITransport(app=admin, client=("127.0.0.1", 50101))
        async with httpx.AsyncClient(transport=local_transport, base_url="http://admin") as client:
            page = await client.get("/")
            assert page.status_code == 200 and "localhost" in page.text
            status_response = await client.get("/api/status")
            payload = status_response.json()
            assert payload["ownership"] == "short-lived-remote-jobs-only"
            assert payload["ttlHours"] == 24
            assert "queue" in payload and "worker" in payload
        remote_transport = httpx.ASGITransport(app=admin, client=("10.0.0.20", 50102))
        async with httpx.AsyncClient(transport=remote_transport, base_url="http://admin") as client:
            assert (await client.get("/api/status")).status_code == 403

        with sqlite3.connect(settings.database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"jobs", "events", "logs"}.issubset(tables)
        assert not any(
            token in name.casefold()
            for name in tables
            for token in ("workspace", "character", "qc")
        )
    finally:
        recovered.stop()
