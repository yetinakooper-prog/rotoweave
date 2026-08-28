from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..jobs import JobManager
from ..network import is_loopback_host
from ..workspace_format import (
    WorkspaceChangedError,
    inspect_legacy_workspace,
    migrate_legacy_workspace,
)
from ..workspace_session import WorkspaceSessionManager, choose_workspace_folder


class WorkspacePathRequest(BaseModel):
    root: str = Field(min_length=1, max_length=32767)


class WorkspaceCreateRequest(WorkspacePathRequest):
    name: str = Field(min_length=1, max_length=128)


class WorkspaceOpenRequest(WorkspacePathRequest):
    pass


class WorkspaceValidateRequest(BaseModel):
    full_hash: bool = True


def _require_local(request: Request) -> None:
    if not is_loopback_host(request.client.host if request.client else None):
        raise HTTPException(403, "工作区只能由运行客户端的本机管理。")


def _require_idle(session: WorkspaceSessionManager) -> None:
    if session.has_active_work():
        raise WorkspaceChangedError("当前工作区仍有排队或运行中的任务，暂时不能切换或关闭。")


def register_workspace_routes(
    router: APIRouter,
    session: WorkspaceSessionManager,
    jobs: JobManager,
) -> None:
    @router.get("/workspace")
    def workspace_status(request: Request) -> dict[str, Any]:
        _require_local(request)
        return session.snapshot(expose_path=True)

    @router.post("/workspace/dialog")
    def workspace_dialog(request: Request) -> dict[str, str | None]:
        _require_local(request)
        return {"root": choose_workspace_folder()}

    @router.post("/workspace/create", status_code=201)
    def create_workspace(request: Request, payload: WorkspaceCreateRequest) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        jobs.stop()
        try:
            result = session.create(Path(payload.root), payload.name)
        except Exception:
            if session.snapshot(expose_path=False)["state"] == "Open":
                jobs.start()
            raise
        jobs.start()
        return result

    @router.post("/workspace/open")
    def open_workspace(request: Request, payload: WorkspaceOpenRequest) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        jobs.stop()
        try:
            result = session.open(Path(payload.root))
        except Exception:
            if session.snapshot(expose_path=False)["state"] == "Open":
                jobs.start()
            raise
        jobs.start()
        return result

    @router.post("/workspace/brand-migration/inspect")
    def inspect_workspace_brand(
        request: Request, payload: WorkspaceOpenRequest
    ) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        return inspect_legacy_workspace(Path(payload.root))

    @router.post("/workspace/brand-migration")
    def migrate_workspace_brand(
        request: Request, payload: WorkspaceOpenRequest
    ) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        return migrate_legacy_workspace(Path(payload.root))

    @router.post("/workspace/validate")
    def validate_workspace(request: Request, payload: WorkspaceValidateRequest) -> dict[str, Any]:
        _require_local(request)
        return session.validate(full_hash=payload.full_hash)

    @router.post("/workspace/reload")
    def reload_workspace(request: Request) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        jobs.stop()
        try:
            result = session.reload()
        except Exception:
            jobs.start()
            raise
        jobs.start()
        return result

    @router.post("/workspace/prepare-and-close")
    def prepare_and_close(request: Request) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        jobs.stop()
        try:
            return session.close(prepare=True)
        except Exception:
            jobs.start()
            raise

    @router.post("/workspace/close")
    def close_workspace(request: Request) -> dict[str, Any]:
        _require_local(request)
        _require_idle(session)
        jobs.stop()
        try:
            return session.close(prepare=False)
        except Exception:
            jobs.start()
            raise

    @router.post("/workspace/reveal")
    def reveal_workspace(request: Request) -> dict[str, bool]:
        _require_local(request)
        root = session.root
        if root is None:
            raise HTTPException(409, "当前没有打开的工作区。")
        if os.name != "nt":
            raise HTTPException(501, "仅 Windows 支持在资源管理器中打开。")
        os.startfile(str(root))
        return {"opened": True}
