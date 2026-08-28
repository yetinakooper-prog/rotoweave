from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import SESSION_COOKIE_NAME, Settings, settings as default_settings
from .media import MediaError
from .network import is_loopback_host, origin_is_allowed
from contracts.legacy_compat import LegacyIdentityConflict, compatible_header_value
from contracts.product import HTTP_API_PREFIX
from .jobs import JobManager
from .storage import ObjectStore
from .v4 import create_v4_router
from .workspace_format import (
    WorkspaceChangedError,
    WorkspaceError,
    WorkspaceRevisionConflict,
)
from .workspace_session import (
    REQUEST_API_PATH,
    REQUEST_REVISION_ID,
    REQUEST_WORKSPACE_EPOCH,
    WorkspaceRepositoryGateway,
    WorkspaceSessionManager,
)


PUBLIC_API_PATHS = {
    f"{HTTP_API_PREFIX}/health",
    f"{HTTP_API_PREFIX}/session/bootstrap",
}


@dataclass(slots=True)
class LocalRuntime:
    session: WorkspaceSessionManager
    database: WorkspaceRepositoryGateway
    store: ObjectStore
    jobs: JobManager

    def shutdown(self) -> None:
        self.jobs.stop()
        self.session.shutdown()


def _is_expected_windows_client_disconnect(context: dict[str, Any]) -> bool:
    """Recognize the noisy Proactor callback emitted after a client closes SSE."""

    exception = context.get("exception")
    winerror = getattr(exception, "winerror", None)
    if winerror is None and isinstance(exception, OSError) and exception.args:
        winerror = exception.args[0]
    message = str(context.get("message") or "")
    return (
        isinstance(exception, (ConnectionResetError, ConnectionAbortedError, OSError))
        and winerror in {10053, 10054}
        and "_ProactorBasePipeTransport._call_connection_lost" in message
    )


def _create_lifespan(
    configured: Settings,
    runtime: LocalRuntime,
):
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        previous_exception_handler = loop.get_exception_handler()

        def exception_handler(
            active_loop: asyncio.AbstractEventLoop,
            context: dict[str, Any],
        ) -> None:
            if _is_expected_windows_client_disconnect(context):
                return
            if previous_exception_handler is not None:
                previous_exception_handler(active_loop, context)
            else:
                active_loop.default_exception_handler(context)

        loop.set_exception_handler(exception_handler)
        configured.ensure_directories()
        if runtime.session.open_recent():
            runtime.jobs.start()
        try:
            yield
        finally:
            try:
                runtime.shutdown()
            finally:
                if loop.get_exception_handler() is exception_handler:
                    loop.set_exception_handler(previous_exception_handler)

    return lifespan


def create_app(
    configured: Settings | None = None,
) -> FastAPI:
    configured = configured or default_settings
    session = WorkspaceSessionManager(configured)
    database = WorkspaceRepositoryGateway(session)
    store = ObjectStore(configured, session)
    store.bind_database(database)
    jobs = JobManager(database, store, configured)
    runtime = LocalRuntime(session, database, store, jobs)

    application = FastAPI(
        title="RotoWeave Local API",
        version=__version__,
        description="Loopback-only local service owned by the RotoWeave 4.0 Windows client.",
        docs_url=f"{HTTP_API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{HTTP_API_PREFIX}/openapi.json",
        lifespan=_create_lifespan(
            configured,
            runtime,
        ),
    )
    application.state.settings = configured
    application.state.database = database
    application.state.workspace_session = session
    application.state.store = store
    application.state.jobs = jobs
    application.state.runtime = runtime
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            f"http://127.0.0.1:{configured.port}",
            f"http://localhost:{configured.port}",
        ],
        allow_origin_regex=None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-RotoWeave-Revision-Id"],
    )
    application.include_router(
        create_v4_router(
            database,
            store,
            jobs,
            configured,
            session,
        )
    )

    @application.middleware("http")
    async def enforce_local_session(request: Request, call_next: Any) -> Any:
        client_host = request.client.host if request.client else None
        remote_request = not is_loopback_host(client_host)
        if remote_request:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "RotoWeave 4.0 本地 API 只接受本机请求。",
                    "code": "local_service_only",
                },
            )
        try:
            supplied_header = compatible_header_value(
                request.headers,
                "X-RotoWeave-Session",
                "X-AIFrame-Session",
            )
            expected_revision_id = compatible_header_value(
                request.headers,
                "X-RotoWeave-Revision-Id",
                "X-AIFrame-Revision-Id",
            )
        except LegacyIdentityConflict as exc:
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "code": "identity_conflict"},
            )
        supplied = supplied_header or request.cookies.get(SESSION_COOKIE_NAME) or ""
        if not origin_is_allowed(
            request.headers.get("origin"),
            configured.port,
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "当前来源不在允许的本机或局域网范围内。"},
            )
        if (
            request.url.path.startswith(HTTP_API_PREFIX)
            and request.url.path not in PUBLIC_API_PATHS
            and request.method != "OPTIONS"
            and configured.require_session_token
        ):
            authenticated = hmac.compare_digest(supplied, configured.session_token)
            if not authenticated:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "当前本机会话已失效，请重新打开客户端。"},
                )
        epoch_token = None
        revision_token = None
        api_path_token = None
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path.startswith(HTTP_API_PREFIX)
            and not request.url.path.startswith(f"{HTTP_API_PREFIX}/workspace")
        ):
            epoch_token = REQUEST_WORKSPACE_EPOCH.set(session.epoch)
        try:
            if expected_revision_id:
                request_api_path = request.url.path.removeprefix(HTTP_API_PREFIX)
                revision_token = REQUEST_REVISION_ID.set(expected_revision_id)
                api_path_token = REQUEST_API_PATH.set(request_api_path)
                try:
                    def assert_request_revision() -> str | None:
                        repository = session.require_repository()
                        target = repository.http_revision_target(
                            request_api_path
                        )
                        repository.assert_http_revision(
                            request_api_path,
                            expected_revision_id,
                        )
                        return target

                    revision_target = await asyncio.to_thread(
                        assert_request_revision
                    )
                except WorkspaceChangedError as exc:
                    session.mark_conflict(str(exc))
                    raise
            response = await call_next(request)
            if expected_revision_id and response.status_code < 400:
                def read_response_revision() -> str | None:
                    repository = session.require_repository()
                    current = repository.current_http_revision(request_api_path)
                    return current or repository.current_target_revision(
                        revision_target
                    )

                current_revision = await asyncio.to_thread(
                    read_response_revision
                )
                response.headers["X-RotoWeave-Revision-Id"] = (
                    current_revision or "deleted"
                )
            return response
        except WorkspaceRevisionConflict as exc:
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "code": "revision_conflict"},
            )
        except WorkspaceChangedError as exc:
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "code": "workspace_changed"},
            )
        except WorkspaceError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        finally:
            if epoch_token is not None:
                REQUEST_WORKSPACE_EPOCH.reset(epoch_token)
            if revision_token is not None:
                REQUEST_REVISION_ID.reset(revision_token)
            if api_path_token is not None:
                REQUEST_API_PATH.reset(api_path_token)

    @application.exception_handler(MediaError)
    async def media_exception_handler(_: Request, exc: MediaError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @application.exception_handler(WorkspaceChangedError)
    async def workspace_changed_handler(_: Request, exc: WorkspaceChangedError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "workspace_changed"},
        )

    @application.exception_handler(WorkspaceRevisionConflict)
    async def workspace_revision_conflict_handler(
        _: Request, exc: WorkspaceRevisionConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "revision_conflict"},
        )

    @application.exception_handler(WorkspaceError)
    async def workspace_error_handler(_: Request, exc: WorkspaceError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    frontend_root = next(
        (
            candidate
            for candidate in configured.frontend_candidates
            if (candidate / "index.html").is_file()
        ),
        None,
    )
    if frontend_root:
        application.mount(
            "/", StaticFiles(directory=frontend_root, html=True), name="frontend"
        )
    else:

        @application.get("/")
        async def api_root() -> dict[str, Any]:
            return {
                "name": "RotoWeave Local API",
                "version": __version__,
                "docs": f"{HTTP_API_PREFIX}/docs",
                "frontend": "Run the Vite development server on http://localhost:3000",
            }

    return application


app = create_app()
