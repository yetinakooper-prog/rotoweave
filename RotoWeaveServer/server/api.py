from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError

from contracts.remote_archive import (
    ARCHIVE_SHA256_HEADER,
    IDEMPOTENCY_HEADER,
    LEGACY_PROTOCOL_HEADER,
    PROTOCOL_HEADER,
)
from contracts.legacy_compat import LegacyIdentityConflict, compatible_header_value
from contracts.deployment_bundles import DeploymentBundleManager
from contracts.remote_protocol import (
    RemoteError,
    RemoteErrorCode,
    RemoteJobSubmission,
    RemoteServiceStatus,
)

from .config import NetworkSettingsError, RemoteServerSettings
from .processor import RemoteProcessingError
from .repository import IdempotencyConflict, InvalidQueueOperation, QueueRevisionConflict, utc_now
from .service import RemoteService, TERMINAL_STATES


class RemoteApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: RemoteErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = RemoteError(
            protocolVersion=1,
            code=code,
            message=message,
            retryable=retryable,
            detail=detail,
        )


def _status_code_for_processing(code: str) -> int:
    return {
        "invalid_request": 422,
        "integrity_failed": 422,
        "model_unavailable": 503,
        "gpu_out_of_memory": 503,
    }.get(code, 500)


def _require_remote_protocol(protocol_version: str | None) -> None:
    if protocol_version != "1":
        raise RemoteApiError(
            409,
            "incompatible_protocol",
            "Remote matting protocol version is incompatible.",
            detail={"supported": 1},
        )


def create_remote_app(
    settings: RemoteServerSettings,
    *,
    service: RemoteService | None = None,
    manage_lifecycle: bool = True,
) -> FastAPI:
    remote_service = service or RemoteService(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if manage_lifecycle:
            remote_service.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                remote_service.stop()

    app = FastAPI(
        title="RotoWeave Remote Matting",
        version="4.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.remote_service = remote_service

    @app.middleware("http")
    async def accept_legacy_protocol_header(request: Request, call_next):
        try:
            protocol = compatible_header_value(
                request.headers,
                PROTOCOL_HEADER,
                LEGACY_PROTOCOL_HEADER,
            )
        except LegacyIdentityConflict as exc:
            return JSONResponse(
                status_code=400,
                content={"code": "identity_conflict", "message": str(exc), "retryable": False},
            )
        if protocol is not None and request.headers.get(PROTOCOL_HEADER) is None:
            request.scope["headers"].append(
                (PROTOCOL_HEADER.lower().encode("ascii"), str(protocol).encode("utf-8"))
            )
        return await call_next(request)

    @app.exception_handler(RemoteApiError)
    async def remote_error_handler(_request: Request, exc: RemoteApiError):
        headers = {}
        if exc.status_code == 503:
            headers["Retry-After"] = "5"
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.error.model_dump(mode="json"),
            headers=headers or None,
        )

    @app.get("/api/matting/v1/status", response_model=RemoteServiceStatus)
    async def connection_status(
        protocol_version: str | None = Header(default=None, alias=PROTOCOL_HEADER),
    ):
        _require_remote_protocol(protocol_version)
        return remote_service.connection_status()

    @app.post("/api/matting/v1/jobs", status_code=202)
    async def submit_job(
        request: Request,
        submission: str = Form(...),
        archive: UploadFile = File(...),
        protocol_version: str | None = Header(default=None, alias=PROTOCOL_HEADER),
        idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ):
        _require_remote_protocol(protocol_version)
        if not idempotency_key:
            raise RemoteApiError(422, "invalid_request", "Idempotency-Key is required.")
        try:
            parsed = RemoteJobSubmission.model_validate_json(submission)
        except ValidationError as exc:
            raise RemoteApiError(
                422,
                "invalid_request",
                "Submission does not match remote protocol v1.",
                detail={"errors": exc.errors(include_url=False)},
            ) from exc
        upload_root = settings.data_root / "uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        temporary = upload_root / f"{uuid.uuid4().hex}.zip.part"
        total = 0
        try:
            with temporary.open("wb") as handle:
                while chunk := await archive.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings.max_upload_bytes:
                        raise RemoteApiError(413, "invalid_request", "Input archive exceeds server limit.")
                    handle.write(chunk)
            try:
                job, _created = remote_service.submit(parsed, temporary, idempotency_key)
            except IdempotencyConflict as exc:
                raise RemoteApiError(409, "conflict", str(exc)) from exc
            except RemoteProcessingError as exc:
                raise RemoteApiError(
                    _status_code_for_processing(exc.code),
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                    detail=exc.detail or None,
                ) from exc
            status = remote_service.status(str(job["id"]))
            if status is None:
                raise RemoteApiError(500, "internal_error", "Accepted job disappeared.", retryable=True)
            return status.model_dump(mode="json")
        finally:
            temporary.unlink(missing_ok=True)
            await archive.close()

    @app.get("/api/matting/v1/jobs/{job_id}")
    async def get_job(
        job_id: str,
        protocol_version: str | None = Header(default=None, alias=PROTOCOL_HEADER),
    ):
        _require_remote_protocol(protocol_version)
        status = remote_service.status(job_id)
        if status is None:
            raise RemoteApiError(404, "not_found", "Remote job does not exist.")
        return status.model_dump(mode="json")

    @app.post("/api/matting/v1/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        protocol_version: str | None = Header(default=None, alias=PROTOCOL_HEADER),
    ):
        _require_remote_protocol(protocol_version)
        status = remote_service.cancel(job_id)
        if status is None:
            raise RemoteApiError(404, "not_found", "Remote job does not exist.")
        return status.model_dump(mode="json")

    @app.get("/api/matting/v1/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        request: Request,
        protocol_version: str | None = Header(default=None, alias=PROTOCOL_HEADER),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        _require_remote_protocol(protocol_version)
        if remote_service.status(job_id) is None:
            raise RemoteApiError(404, "not_found", "Remote job does not exist.")
        sequence = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1

        async def stream() -> AsyncIterator[str]:
            cursor = sequence
            heartbeat_at = asyncio.get_running_loop().time()
            while True:
                events = remote_service.repository.events_after(job_id, cursor)
                for event in events:
                    cursor = int(event["sequence"])
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    if str(event["state"]) in TERMINAL_STATES:
                        return
                status = remote_service.status(job_id)
                if status is None or status.state.value in TERMINAL_STATES:
                    return
                if await request.is_disconnected():
                    return
                now = asyncio.get_running_loop().time()
                if now - heartbeat_at >= 10:
                    heartbeat_at = now
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/matting/v1/jobs/{job_id}/result")
    async def get_result(
        job_id: str,
        protocol_version: str | None = Header(default=None, alias=PROTOCOL_HEADER),
    ):
        _require_remote_protocol(protocol_version)
        job = remote_service.repository.get(job_id)
        if job is None:
            raise RemoteApiError(404, "not_found", "Remote job does not exist.")
        if job["state"] != "completed":
            raise RemoteApiError(409, "conflict", "Remote result is not ready.", retryable=True)
        result = Path(str(job.get("result_path") or ""))
        digest = str(job.get("result_sha256") or "")
        if not result.is_file() or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RemoteApiError(500, "integrity_failed", "Remote result is unavailable.")
        return FileResponse(
            result,
            media_type="application/zip",
            filename=f"{job_id}.zip",
            headers={ARCHIVE_SHA256_HEADER: digest},
        )

    return app


def create_admin_app(service: RemoteService) -> FastAPI:
    app = FastAPI(
        title="RotoWeave Remote Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    csrf_token = secrets.token_urlsafe(32)
    deployment_manager = DeploymentBundleManager(Path(__file__).resolve().parents[2], "server")
    runtime_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    static_root = (
        runtime_root / "server-admin"
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1] / "server-admin" / "dist"
    )

    def require_loopback(request: Request) -> None:
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "testclient"}:
            raise RemoteApiError(403, "unauthorized", "Administration is localhost-only.")

    def require_admin_request(request: Request, *, write: bool = False) -> None:
        require_loopback(request)
        host = (request.headers.get("host") or "").split(":", 1)[0].strip("[]").casefold()
        if host not in {"127.0.0.1", "localhost", "::1", "admin", "testserver"}:
            raise RemoteApiError(403, "unauthorized", "Administration Host is invalid.")
        origin = request.headers.get("origin")
        if origin:
            allowed = (
                origin.startswith("http://127.0.0.1:")
                or origin.startswith("http://localhost:")
                or origin in {"http://admin", "http://testserver"}
            )
            if not allowed:
                raise RemoteApiError(403, "unauthorized", "Administration Origin is invalid.")
        if write:
            try:
                supplied_csrf = compatible_header_value(
                    request.headers,
                    "X-RotoWeave-Admin-CSRF",
                    "X-AIFrame-Admin-CSRF",
                )
            except LegacyIdentityConflict as exc:
                raise RemoteApiError(
                    400,
                    "invalid_request",
                    str(exc),
                    detail={"code": "identity_conflict"},
                ) from exc
            if not hmac.compare_digest(
                str(supplied_csrf or "").encode("utf-8"),
                csrf_token.encode("utf-8"),
            ):
                raise RemoteApiError(403, "unauthorized", "Administration CSRF token is missing or invalid.")

    @app.exception_handler(RemoteApiError)
    async def admin_error_handler(_request: Request, exc: RemoteApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.error.model_dump(mode="json"))

    @app.exception_handler(QueueRevisionConflict)
    async def revision_error(_request: Request, exc: QueueRevisionConflict):
        return JSONResponse(status_code=409, content={"code": "queue_revision_conflict", "message": str(exc), "retryable": True})

    @app.exception_handler(InvalidQueueOperation)
    async def operation_error(_request: Request, exc: InvalidQueueOperation):
        return JSONResponse(status_code=409, content={"code": "invalid_operation", "message": str(exc), "retryable": False})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        require_admin_request(request)
        index_path = static_root / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return HTMLResponse("<!doctype html><meta charset='utf-8'><title>RotoWeave</title><body style='background:#0b1017;color:white;font-family:system-ui;padding:32px'><h1>服务端后台资源尚未构建</h1><p>请运行 <code>npm run build:server-admin</code> 后重启服务。</p></body>")

    @app.get("/assets/{asset_path:path}")
    async def assets(asset_path: str, request: Request):
        require_admin_request(request)
        target = (static_root / "assets" / asset_path).resolve(strict=False)
        try:
            target.relative_to((static_root / "assets").resolve())
        except ValueError as exc:
            raise RemoteApiError(404, "not_found", "Asset does not exist.") from exc
        if not target.is_file():
            raise RemoteApiError(404, "not_found", "Asset does not exist.")
        return FileResponse(target)

    @app.get("/api/status")
    async def status(request: Request):
        require_admin_request(request)
        return service.admin_status()

    @app.post("/api/cleanup")
    async def cleanup(request: Request):
        require_admin_request(request, write=True)
        return service.cleanup_expired()

    def camelize(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), str(key))
                result[name] = camelize(item)
            return result
        if isinstance(value, list):
            return [camelize(item) for item in value]
        return value

    def model_center_error(exc: Exception) -> RemoteApiError:
        if isinstance(exc, KeyError):
            return RemoteApiError(404, "not_found", "模型中心实体不存在。")
        return RemoteApiError(409 if "正在执行" in str(exc) or "不可" in str(exc) else 422, "invalid_request", str(exc))

    @app.get("/api/admin/v2/session")
    async def admin_session_v2(request: Request):
        require_admin_request(request)
        return {"csrfToken": csrf_token, "expires": "process", "localhostOnly": True}

    @app.get("/api/admin/v2/deployment-bundles/plan")
    async def deployment_bundle_plan_v2(request: Request):
        require_admin_request(request)
        try:
            return deployment_manager.plan()
        except Exception as exc:
            raise RemoteApiError(409, "conflict", str(exc)) from exc

    @app.post("/api/admin/v2/deployment-bundles/output-directory-dialog")
    async def deployment_bundle_directory_v2(request: Request):
        require_admin_request(request, write=True)
        try:
            return deployment_manager.select_directory()
        except Exception as exc:
            raise RemoteApiError(409, "conflict", str(exc)) from exc

    @app.post("/api/admin/v2/deployment-bundles/exports", status_code=202)
    async def start_deployment_bundle_export_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        try:
            return deployment_manager.start(str(body.get("selectionToken") or ""))
        except ValueError as exc:
            raise RemoteApiError(409, "conflict", str(exc)) from exc

    @app.get("/api/admin/v2/deployment-bundles/exports/{export_id}")
    async def deployment_bundle_export_v2(export_id: str, request: Request):
        require_admin_request(request)
        try:
            return deployment_manager.get(export_id)
        except KeyError as exc:
            raise RemoteApiError(404, "not_found", "部署包导出任务不存在。") from exc

    @app.delete("/api/admin/v2/deployment-bundles/exports/{export_id}")
    async def cancel_deployment_bundle_export_v2(export_id: str, request: Request):
        require_admin_request(request, write=True)
        try:
            return deployment_manager.cancel(export_id)
        except KeyError as exc:
            raise RemoteApiError(404, "not_found", "部署包导出任务不存在。") from exc

    @app.post("/api/admin/v2/deployment-bundles/exports/{export_id}/reveal")
    async def reveal_deployment_bundle_export_v2(export_id: str, request: Request):
        require_admin_request(request, write=True)
        try:
            return deployment_manager.reveal(export_id)
        except KeyError as exc:
            raise RemoteApiError(404, "not_found", "部署包导出任务不存在。") from exc
        except ValueError as exc:
            raise RemoteApiError(409, "conflict", str(exc)) from exc

    @app.get("/api/admin/v2/overview")
    async def overview_v2(request: Request):
        require_admin_request(request)
        return camelize(service.admin_status())

    @app.get("/api/admin/v2/network-settings")
    async def network_settings_v2(request: Request):
        require_admin_request(request)
        return camelize(service.network_status())

    @app.put("/api/admin/v2/network-settings")
    async def save_network_settings_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        try:
            if not isinstance(body, dict):
                raise NetworkSettingsError("invalid_request", "请求正文必须是 JSON 对象。")
            if "apiHost" in body:
                raise NetworkSettingsError("api_host_read_only", "服务地址由系统自动识别，不允许修改。")
            return camelize(service.save_network_settings(body.get("apiPort")))
        except NetworkSettingsError as exc:
            raise RemoteApiError(
                exc.status_code,
                "conflict" if exc.status_code == 409 else "invalid_request",
                str(exc),
                detail={"reason": exc.code},
            ) from exc

    @app.get("/api/admin/v2/model-center")
    async def model_center_v2(request: Request):
        require_admin_request(request)
        return service.model_center.snapshot()

    @app.post("/api/admin/v2/model-selections/folder-dialog")
    async def select_model_folder_v2(request: Request):
        require_admin_request(request, write=True)
        try:
            selected = await asyncio.to_thread(service.model_center.choose_folder)
            if selected is None:
                return {"cancelled": True}
            return {
                "cancelled": False,
                "operation": service.model_center.select_folder(selected),
            }
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.post("/api/admin/v2/model-selections/{role}/file-dialog")
    async def select_model_file_v2(role: str, request: Request):
        require_admin_request(request, write=True)
        try:
            selected = await asyncio.to_thread(service.model_center.choose_file, role)
            if selected is None:
                return {"cancelled": True}
            return {
                "cancelled": False,
                "operation": service.model_center.select_file(role, selected),
            }
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.post("/api/admin/v2/model-selections/default")
    async def select_default_models_v2(request: Request):
        require_admin_request(request, write=True)
        try:
            return service.model_center.select_default()
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.delete("/api/admin/v2/model-bindings/{role}")
    async def unbind_model_v2(role: str, request: Request):
        require_admin_request(request, write=True)
        try:
            return service.model_center.unbind(role)
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.put("/api/admin/v2/model-bindings/{role}", include_in_schema=False)
    async def retired_asset_binding_v2(role: str, request: Request):
        require_admin_request(request, write=True)
        raise RemoteApiError(404, "not_found", "按候选资产绑定模型的接口已退役。")

    @app.post("/api/admin/v2/model-configurations/draft/verify")
    async def verify_model_configuration_v2(request: Request):
        require_admin_request(request, write=True)
        try:
            return service.model_center.verify_draft()
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.post("/api/admin/v2/model-configurations/draft/self-test")
    async def self_test_model_configuration_v2(request: Request):
        require_admin_request(request, write=True)
        try:
            return service.model_center.self_test()
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.post("/api/admin/v2/model-configurations/draft/activate")
    async def activate_model_configuration_v2(request: Request):
        require_admin_request(request, write=True)
        try:
            return service.model_center.activate()
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.get("/api/admin/v2/model-operations/{operation_id}")
    async def model_operation_v2(operation_id: str, request: Request):
        require_admin_request(request)
        try:
            return service.model_center.operation(operation_id)
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.delete("/api/admin/v2/model-operations/{operation_id}")
    async def cancel_model_operation_v2(operation_id: str, request: Request):
        require_admin_request(request, write=True)
        try:
            return service.model_center.cancel_operation(operation_id)
        except Exception as exc:
            raise model_center_error(exc) from exc

    @app.get("/api/admin/v2/queue")
    async def queue_v2(request: Request, state: str | None = None, profile: str | None = None, limit: int = 100, offset: int = 0):
        require_admin_request(request)
        snapshot = service.queue_snapshot(state=state, limit=limit, offset=offset)
        if profile:
            snapshot["items"] = [item for item in snapshot["items"] if item.get("quality_profile") == profile]
            snapshot["total"] = len(snapshot["items"])
        return camelize(snapshot)

    @app.post("/api/admin/v2/queue/pause")
    async def pause_queue_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        return service.pause_queue(True, body.get("revision"))

    @app.post("/api/admin/v2/queue/resume")
    async def resume_queue_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        return service.pause_queue(False, body.get("revision"))

    @app.post("/api/admin/v2/queue/reorder")
    async def reorder_queue_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        return service.reorder_queue([str(item) for item in body.get("jobIds") or []], int(body.get("revision")))

    @app.post("/api/admin/v2/queue/emergency-stop")
    async def emergency_stop_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        if body.get("confirm") != "EMERGENCY_STOP":
            raise RemoteApiError(422, "invalid_request", "Emergency stop requires explicit confirmation.")
        return service.emergency_stop()

    @app.post("/api/admin/v2/queue/cleanup")
    async def cleanup_queue_v2(request: Request):
        require_admin_request(request, write=True)
        body = await request.json()
        states = {str(item) for item in body.get("states") or ["completed", "failed", "cancelled"]}
        if not states.issubset(TERMINAL_STATES):
            raise RemoteApiError(422, "invalid_request", "Queue cleanup only accepts terminal states.")
        removed = 0
        for state in sorted(states):
            for job in service.queue_snapshot(state=state, limit=500)["items"]:
                removed += int(service.delete_terminal_job(str(job["id"])))
        return {"removed": removed, "states": sorted(states)}

    @app.get("/api/admin/v2/jobs/{job_id}")
    async def job_detail_v2(job_id: str, request: Request):
        require_admin_request(request)
        job = service.repository.get(job_id)
        if job is None:
            raise RemoteApiError(404, "not_found", "Job does not exist.")
        return camelize({"job": job, "events": service.repository.events_after(job_id, -1)})

    @app.post("/api/admin/v2/jobs/{job_id}/cancel")
    async def cancel_job_v2(job_id: str, request: Request):
        require_admin_request(request, write=True)
        status = service.cancel(job_id)
        if status is None:
            raise RemoteApiError(404, "not_found", "Job does not exist.")
        return status.model_dump(mode="json")

    @app.post("/api/admin/v2/jobs/{job_id}/retry")
    async def retry_job_v2(job_id: str, request: Request):
        require_admin_request(request, write=True)
        return camelize(service.retry_job(job_id))

    @app.delete("/api/admin/v2/jobs/{job_id}")
    async def delete_job_v2(job_id: str, request: Request):
        require_admin_request(request, write=True)
        if not service.delete_terminal_job(job_id):
            raise RemoteApiError(404, "not_found", "Job does not exist.")
        return {"deleted": True, "jobId": job_id}

    @app.get("/api/admin/v2/logs")
    async def logs_v2(request: Request, level: str | None = None, component: str | None = None,
                      event: str | None = None, jobId: str | None = None, text: str | None = None,
                      since: str | None = None, until: str | None = None, profile: str | None = None,
                      modelRole: str | None = None, configurationDigest: str | None = None,
                      operationId: str | None = None, limit: int = 200, offset: int = 0):
        require_admin_request(request)
        return camelize(service.repository.query_logs(
            level=level, component=component, event=event, job_id=jobId,
            text=text, profile=profile, model_role=modelRole,
            configuration_digest=configurationDigest, operation_id=operationId,
            since=since, until=until, limit=limit, offset=offset,
        ))

    @app.get("/api/admin/v2/logs/export")
    async def export_logs_v2(request: Request, format: str = "ndjson"):
        require_admin_request(request)
        records = camelize(service.repository.query_logs(limit=1000)["items"])
        target = service.settings.logs_root / f"operational-export-{int(time.time())}.{format}"
        if format == "csv":
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["id", "createdAt", "level", "component", "event", "jobId", "detail"])
                for item in records:
                    writer.writerow([item["id"], item["createdAt"], item["level"], item["component"], item["event"], item.get("jobId"), json.dumps(item["detail"], ensure_ascii=False)])
        else:
            target = target.with_suffix(".ndjson")
            with target.open("w", encoding="utf-8") as handle:
                for item in records:
                    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        return FileResponse(target, filename=target.name)

    def redact_local_paths(value, key: str = ""):
        if isinstance(value, dict):
            return {name: redact_local_paths(item, str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [redact_local_paths(item, key) for item in value]
        normalized_key = key.casefold().replace("_", "")
        if isinstance(value, str) and (normalized_key == "python" or normalized_key.endswith("path")):
            return "[LOCAL_PATH_REDACTED]"
        return value

    @app.get("/api/admin/v2/logs/diagnostic.zip")
    async def diagnostic_zip_v2(request: Request):
        require_admin_request(request)
        target = service.settings.logs_root / f"diagnostic-{int(time.time())}.zip"
        overview = redact_local_paths(camelize(service.admin_status()))
        records = redact_local_paths(camelize(service.repository.query_logs(limit=1000)["items"]))
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("overview.json", json.dumps(overview, ensure_ascii=False, indent=2))
            archive.writestr("logs.ndjson", "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
        return FileResponse(target, filename=target.name)

    @app.post("/api/admin/v2/logs/cleanup")
    async def cleanup_logs_v2(request: Request):
        require_admin_request(request, write=True)
        return service.repository.cleanup_logs(service.settings.log_retention_days, service.settings.log_max_rows)

    @app.get("/api/admin/v2/events")
    async def admin_events_v2(request: Request, last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
        require_admin_request(request)
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else int(time.time() * 1000)

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            previous: dict[str, str] = {}
            while not await request.is_disconnected():
                status = service.admin_status()
                entities = {
                    "overview": {key: status.get(key) for key in ("worker", "queue", "startup", "disk", "cleanup")},
                    "modelCenter": status.get("modelCenter"),
                }
                changed = []
                for name, value in entities.items():
                    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                    if previous.get(name) != digest:
                        previous[name] = digest
                        changed.append(name)
                if changed:
                    cursor += 1
                    payload = {"sequence": cursor, "entities": changed}
                    yield f"id: {cursor}\nevent: entities-changed\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
