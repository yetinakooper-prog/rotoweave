from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

import cv2
import httpx

from contracts.product import REMOTE_MATTING_API_PREFIX, REMOTE_MATTING_API_VERSION
from .remote_protocol import (
    RemoteError,
    RemoteInputFrame,
    RemoteJobState,
    RemoteJobStatus,
    RemoteJobSubmission,
    RemoteProgressEvent,
    RemoteQuality,
    RemoteResultManifest,
    RemoteServiceStatus,
)
from .workspace_format import resolve_workspace_path, sha256_file
from .workspace_repository import WorkspaceRepository


IDEMPOTENCY_HEADER = "Idempotency-Key"
PROTOCOL_HEADER = "X-RotoWeave-Protocol-Version"
ARCHIVE_SHA256_HEADER = "X-Archive-SHA256"
RESULT_MANIFEST_PATH = "result.json"
MAX_RESULT_BYTES = 32 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
_TRUSTED_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _safe_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RemoteIntegrityError(f"远程结果包含不安全路径：{name!r}")
    return pure.as_posix()


def result_payload_sha256(
    manifest: RemoteResultManifest,
    read_member: Callable[[str], bytes],
) -> str:
    payload = manifest.model_dump(mode="json")
    payload["archiveSha256"] = ""
    digest = hashlib.sha256(canonical_json_bytes(payload))
    for frame in sorted(manifest.frames, key=lambda item: item.ordinal):
        for member in (frame.rgbaPath, frame.emissionPath):
            if member:
                digest.update(member.encode("utf-8"))
                digest.update(b"\0")
                digest.update(read_member(member))
    return digest.hexdigest()


class RemoteMattingError(RuntimeError):
    pass


class RemoteIntegrityError(RemoteMattingError):
    pass


class RemoteResponseError(RemoteMattingError):
    def __init__(self, status_code: int, error: RemoteError):
        super().__init__(error.message)
        self.status_code = status_code
        self.error = error
        self.retryable = error.retryable


@dataclass(frozen=True, slots=True)
class RemoteMattingConfig:
    service_url: str
    timeout_seconds: float = 30.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        parsed = urlsplit(self.service_url)
        if (
            parsed.scheme.lower() != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("远程抠图服务地址必须是无内嵌凭据的 HTTP URL。")
        if parsed.path.rstrip("/") not in {"", REMOTE_MATTING_API_PREFIX}:
            raise ValueError("远程抠图服务地址不能包含协议前缀之外的路径。")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("远程抠图服务地址必须使用固定 IPv4。") from exc
        if (
            not isinstance(address, ipaddress.IPv4Address)
            or address.is_unspecified
            or address.is_multicast
            or address.is_link_local
            or not (
                address.is_loopback
                or any(address in network for network in _TRUSTED_LAN_NETWORKS)
            )
        ):
            raise ValueError("远程抠图服务地址必须是回环或 RFC1918 私网 IPv4。")
        if self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ValueError("远程抠图超时和重试次数无效。")

    @property
    def api_url(self) -> str:
        base = self.service_url.rstrip("/")
        return base if base.endswith(REMOTE_MATTING_API_PREFIX) else base + REMOTE_MATTING_API_PREFIX


@dataclass(frozen=True, slots=True)
class PreparedRemoteSubmission:
    submission: RemoteJobSubmission
    archive_path: Path
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class DownloadedRemoteResult:
    archive_path: Path
    transport_sha256: str
    manifest: RemoteResultManifest


def prepare_remote_submission(
    repository: WorkspaceRepository,
    source_id: str,
    quality: RemoteQuality | str,
    settings: dict[str, Any],
    archive_path: Path,
    *,
    frame_indexes: list[int],
) -> PreparedRemoteSubmission:
    source = repository.get_material_source(source_id)
    if source is None:
        raise KeyError(source_id)
    source_frames = source.get("frames") or []
    if not source_frames:
        raise RemoteMattingError("远程抠图素材没有源帧。")
    selected_indexes = list(frame_indexes)
    if not selected_indexes:
        raise RemoteMattingError("远程抠图至少需要一个源帧。")
    if selected_indexes != sorted(selected_indexes) or len(selected_indexes) != len(set(selected_indexes)) or any(
        index < 0 or index >= len(source_frames) for index in selected_indexes
    ):
        raise RemoteMattingError("远程抠图源帧选择无效。")
    frames = [source_frames[index] for index in selected_indexes]
    archive_path = archive_path.resolve(strict=False)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    part = archive_path.with_suffix(archive_path.suffix + ".part")
    part.unlink(missing_ok=True)
    records: list[RemoteInputFrame] = []
    try:
        with zipfile.ZipFile(
            part,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for ordinal, (source_index, frame) in enumerate(
                zip(selected_indexes, frames, strict=True)
            ):
                linear = frame.get("linear") or {}
                logical_path = str(linear.get("path") or frame.get("path") or "")
                source_path = resolve_workspace_path(repository.root, logical_path)
                suffix = source_path.suffix.lower().lstrip(".") or "png"
                member = f"frames/{ordinal:06d}.{suffix}"
                digest = sha256_file(source_path)
                declared = str(linear.get("sha256") or frame.get("sha256") or "")
                if declared and declared != digest:
                    raise RemoteIntegrityError(f"源帧 {source_index} 的内容哈希已变化。")
                records.append(
                    RemoteInputFrame(
                        frameId=str(frame["id"]),
                        ordinal=ordinal,
                        ptsUs=int(frame.get("ptsUs") or 0),
                        durationUs=int(frame.get("durationUs") or 1),
                        width=int(frame.get("width") or 1),
                        height=int(frame.get("height") or 1),
                        archivePath=member,
                        sha256=digest,
                    )
                )
                archive.write(source_path, member)
        part.replace(archive_path)
    except Exception:
        part.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)
        raise
    frame_payload = [item.model_dump(mode="json") for item in records]
    video = source.get("video") or {}
    submission = RemoteJobSubmission(
        protocolVersion=REMOTE_MATTING_API_VERSION,
        materialId=source_id,
        materialSha256=str(video.get("sha256") or ""),
        quality=RemoteQuality(quality),
        frameCount=len(records),
        framesManifestSha256=canonical_sha256(frame_payload),
        archiveSha256=sha256_file(archive_path),
        frames=records,
        settings=settings,
    )
    idempotency_key = canonical_sha256(submission.model_dump(mode="json"))
    return PreparedRemoteSubmission(submission, archive_path, idempotency_key)


class RemoteMattingClient:
    def __init__(
        self,
        config: RemoteMattingConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_url,
            headers={
                PROTOCOL_HEADER: str(REMOTE_MATTING_API_VERSION),
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> "RemoteMattingClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe(self) -> RemoteServiceStatus:
        response = await self._client.get("status")
        await self._raise_response(response)
        try:
            return RemoteServiceStatus.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise RemoteIntegrityError("远程服务状态响应不符合协议 v1。") from exc

    @staticmethod
    async def _raise_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            payload = response.json()
            error = RemoteError.model_validate(payload)
        except Exception:
            error = RemoteError(
                protocolVersion=REMOTE_MATTING_API_VERSION,
                code="internal_error",
                message=f"远程抠图服务返回 HTTP {response.status_code}。",
                retryable=response.status_code >= 500,
                detail=None,
            )
        raise RemoteResponseError(response.status_code, error)

    async def _retry(self, operation: Callable[[], Any]) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await operation()
                if response.status_code not in {502, 503, 504}:
                    await self._raise_response(response)
                    return response
                await self._raise_response(response)
            except (httpx.TransportError, RemoteResponseError) as exc:
                last = exc
                retryable = not isinstance(exc, RemoteResponseError) or exc.retryable
                if not retryable or attempt >= self.config.max_retries:
                    raise
                await asyncio.sleep(min(0.1 * (2**attempt), 1.0))
        raise RemoteMattingError("远程请求重试失败。") from last

    async def submit(self, prepared: PreparedRemoteSubmission) -> RemoteJobStatus:
        if sha256_file(prepared.archive_path) != prepared.submission.archiveSha256:
            raise RemoteIntegrityError("待上传帧包在提交前已变化。")

        async def request() -> httpx.Response:
            with prepared.archive_path.open("rb") as handle:
                return await self._client.post(
                    "jobs",
                    headers={IDEMPOTENCY_HEADER: prepared.idempotency_key},
                    files={
                        "submission": (
                            None,
                            prepared.submission.model_dump_json(),
                            "application/json",
                        ),
                        "archive": ("frames.zip", handle, "application/zip"),
                    },
                )

        response = await self._retry(request)
        return RemoteJobStatus.model_validate(response.json())

    async def status(self, job_id: str) -> RemoteJobStatus:
        response = await self._retry(lambda: self._client.get(f"jobs/{job_id}"))
        return RemoteJobStatus.model_validate(response.json())

    async def cancel(self, job_id: str) -> RemoteJobStatus:
        response = await self._retry(
            lambda: self._client.post(f"jobs/{job_id}/cancel")
        )
        return RemoteJobStatus.model_validate(response.json())

    async def events(
        self,
        job_id: str,
        *,
        after_sequence: int = -1,
        reconnects: int = 8,
    ) -> AsyncIterator[RemoteProgressEvent]:
        last_sequence = after_sequence
        attempts = 0
        while attempts <= reconnects:
            headers = {"Accept": "text/event-stream"}
            if last_sequence >= 0:
                headers["Last-Event-ID"] = str(last_sequence)
            try:
                async with self._client.stream(
                    "GET", f"jobs/{job_id}/events", headers=headers
                ) as response:
                    await self._raise_response(response)
                    event_id: int | None = None
                    data: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith("id:"):
                            raw = line[3:].strip()
                            event_id = int(raw) if raw.isdigit() else None
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line == "" and data:
                            event = RemoteProgressEvent.model_validate_json("\n".join(data))
                            data = []
                            sequence = event_id if event_id is not None else event.sequence
                            event_id = None
                            if sequence <= last_sequence:
                                continue
                            if sequence != event.sequence:
                                raise RemoteIntegrityError("SSE 事件 ID 与正文序号不一致。")
                            last_sequence = sequence
                            yield event
                            if event.state in {
                                RemoteJobState.COMPLETED,
                                RemoteJobState.FAILED,
                                RemoteJobState.CANCELLED,
                            }:
                                return
                attempts += 1
            except (httpx.TransportError, httpx.TimeoutException):
                attempts += 1
            if attempts > reconnects:
                break
            await asyncio.sleep(min(0.1 * (2 ** max(0, attempts - 1)), 1.0))
        raise RemoteMattingError("远程进度连接中断且已超过重连次数。")

    async def download_result(
        self, job_id: str, destination: Path
    ) -> DownloadedRemoteResult:
        destination = destination.resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        for attempt in range(self.config.max_retries + 1):
            part.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            digest = hashlib.sha256()
            total = 0
            try:
                async with self._client.stream(
                    "GET",
                    f"jobs/{job_id}/result",
                    headers={"Accept": "application/zip"},
                ) as response:
                    await self._raise_response(response)
                    declared = str(
                        response.headers.get(ARCHIVE_SHA256_HEADER) or ""
                    ).lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", declared):
                        raise RemoteIntegrityError("远程结果缺少有效的传输 SHA-256。")
                    with part.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_RESULT_BYTES:
                                raise RemoteIntegrityError(
                                    "远程结果超过客户端大小上限。"
                                )
                            digest.update(chunk)
                            handle.write(chunk)
                actual = digest.hexdigest()
                if actual != declared:
                    raise RemoteIntegrityError("远程结果传输 SHA-256 校验失败。")
                part.replace(destination)
                manifest = inspect_remote_result(destination)
                return DownloadedRemoteResult(destination, actual, manifest)
            except (httpx.TransportError, RemoteResponseError) as exc:
                part.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                retryable = not isinstance(exc, RemoteResponseError) or exc.retryable
                if not retryable or attempt >= self.config.max_retries:
                    raise
                await asyncio.sleep(min(0.1 * (2**attempt), 1.0))
            except Exception:
                part.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        raise RemoteMattingError("远程结果下载重试失败。")


def inspect_remote_result(archive_path: Path) -> RemoteResultManifest:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise RemoteIntegrityError("远程结果文件数量超过上限。")
            if sum(int(info.file_size) for info in infos) > MAX_RESULT_BYTES:
                raise RemoteIntegrityError("远程结果解压后超过客户端大小上限。")
            raw_names = [
                _safe_member(info.filename) for info in infos if not info.is_dir()
            ]
            if len(raw_names) != len(set(raw_names)):
                raise RemoteIntegrityError("远程结果包含重复文件名。")
            names = set(raw_names)
            if RESULT_MANIFEST_PATH not in names:
                raise RemoteIntegrityError("远程结果缺少 result.json。")
            manifest = RemoteResultManifest.model_validate_json(
                archive.read(RESULT_MANIFEST_PATH)
            )
            if manifest.frameCount != len(manifest.frames):
                raise RemoteIntegrityError("远程结果帧数与清单不一致。")
            ordered = sorted(manifest.frames, key=lambda item: item.ordinal)
            if [item.ordinal for item in ordered] != list(range(manifest.frameCount)):
                raise RemoteIntegrityError("远程结果帧序号不连续。")
            for frame in ordered:
                for member, expected in (
                    (frame.rgbaPath, frame.rgbaSha256),
                    (frame.emissionPath, frame.emissionSha256),
                ):
                    if member is None:
                        if expected is not None:
                            raise RemoteIntegrityError("远程结果图层路径与哈希不成对。")
                        continue
                    if expected is None or _safe_member(member) not in names:
                        raise RemoteIntegrityError(f"远程结果缺少图层：{member}")
                    if hashlib.sha256(archive.read(member)).hexdigest() != expected:
                        raise RemoteIntegrityError(f"远程结果图层哈希失败：{member}")
            frame_payload = [item.model_dump(mode="json") for item in ordered]
            if canonical_sha256(frame_payload) != manifest.frameMappingSha256:
                raise RemoteIntegrityError("远程结果帧映射哈希失败。")
            if result_payload_sha256(manifest, archive.read) != manifest.archiveSha256:
                raise RemoteIntegrityError("远程结果内容清单哈希失败。")
            return manifest
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise RemoteIntegrityError("远程结果不是有效的 ZIP 帧包。") from exc


def publish_remote_result(
    repository: WorkspaceRepository,
    source_id: str,
    downloaded: DownloadedRemoteResult,
    staging_root: Path,
    *,
    expected_revision_id: str,
    expected_source_frame_ids: list[str] | None = None,
) -> dict[str, Any]:
    source = repository.get_material_source(source_id)
    if source is None:
        raise KeyError(source_id)
    manifest = inspect_remote_result(downloaded.archive_path)
    if manifest.materialId != source_id:
        raise RemoteIntegrityError("远程结果素材身份不匹配。")
    if manifest.materialSha256 != str((source.get("video") or {}).get("sha256") or ""):
        raise RemoteIntegrityError("远程结果素材内容哈希不匹配。")
    source_frames = source.get("frames") or []
    expected_ids = (
        [str(item["id"]) for item in source_frames]
        if expected_source_frame_ids is None
        else [str(item) for item in expected_source_frame_ids]
    )
    ordered = sorted(manifest.frames, key=lambda item: item.ordinal)
    if [item.sourceFrameId for item in ordered] != expected_ids:
        raise RemoteIntegrityError("远程结果没有完整映射到本次选择的源帧。")
    staging_root = staging_root.resolve(strict=False)
    if staging_root.exists():
        raise RemoteIntegrityError("远程结果暂存目录已存在。")
    rgba_paths: list[str] = []
    emission_paths: list[str | None] = []
    try:
        staging_root.mkdir(parents=True)
        with zipfile.ZipFile(downloaded.archive_path, "r") as archive:
            for frame in ordered:
                rgba = staging_root / "rgba" / f"{frame.ordinal:06d}.png"
                rgba.parent.mkdir(parents=True, exist_ok=True)
                rgba.write_bytes(archive.read(frame.rgbaPath))
                image = cv2.imread(str(rgba), cv2.IMREAD_UNCHANGED)
                if image is None or image.shape != (frame.height, frame.width, 4):
                    raise RemoteIntegrityError(f"远程 RGBA 帧 {frame.ordinal} 尺寸或通道无效。")
                rgba_paths.append(str(rgba))
                if frame.emissionPath:
                    emission = staging_root / "emission" / f"{frame.ordinal:06d}.png"
                    emission.parent.mkdir(parents=True, exist_ok=True)
                    emission.write_bytes(archive.read(frame.emissionPath))
                    layer = cv2.imread(str(emission), cv2.IMREAD_UNCHANGED)
                    if layer is None or layer.shape[:2] != (frame.height, frame.width):
                        raise RemoteIntegrityError(f"远程特效层 {frame.ordinal} 尺寸无效。")
                    emission_paths.append(str(emission))
                else:
                    emission_paths.append(None)
        variant = repository.publish_material_variant(
            source_id,
            manifest.quality.value,
            rgba_paths,
            {
                **manifest.settings,
                "remote": {
                    "protocolVersion": manifest.protocolVersion,
                    "jobId": manifest.jobId,
                    "model": manifest.model,
                    "transportArchiveSha256": downloaded.transport_sha256,
                    "contentSha256": manifest.archiveSha256,
                    "frameMappingSha256": manifest.frameMappingSha256,
                },
            },
            emission_paths=emission_paths,
            expected_revision_id=expected_revision_id,
            source_frame_ids=expected_ids,
        )
        return variant
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
