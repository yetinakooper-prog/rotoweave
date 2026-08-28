from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from contracts.deployment_bundles import DeploymentBundleManager

from ..network import is_loopback_host


class DeploymentExportRequest(BaseModel):
    selectionToken: str = Field(min_length=16, max_length=128)


def _require_local(request: Request) -> None:
    if not is_loopback_host(request.client.host if request.client else None):
        raise HTTPException(403, "部署包只能由运行客户端的本机管理。")


def register_deployment_bundle_routes(router: APIRouter) -> DeploymentBundleManager:
    project_root = Path(__file__).resolve().parents[4]
    manager = DeploymentBundleManager(project_root, "client")

    @router.get("/deployment-bundles/plan")
    def deployment_bundle_plan(request: Request) -> dict[str, Any]:
        _require_local(request)
        try:
            return manager.plan()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/deployment-bundles/output-directory-dialog")
    def deployment_bundle_directory(request: Request) -> dict[str, Any]:
        _require_local(request)
        try:
            return manager.select_directory()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/deployment-bundles/exports", status_code=202)
    def start_deployment_bundle_export(request: Request, payload: DeploymentExportRequest) -> dict[str, Any]:
        _require_local(request)
        try:
            return manager.start(payload.selectionToken)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/deployment-bundles/exports/{export_id}")
    def deployment_bundle_export(export_id: str, request: Request) -> dict[str, Any]:
        _require_local(request)
        try:
            return manager.get(export_id)
        except KeyError as exc:
            raise HTTPException(404, "部署包导出任务不存在。") from exc

    @router.delete("/deployment-bundles/exports/{export_id}")
    def cancel_deployment_bundle_export(export_id: str, request: Request) -> dict[str, Any]:
        _require_local(request)
        try:
            return manager.cancel(export_id)
        except KeyError as exc:
            raise HTTPException(404, "部署包导出任务不存在。") from exc

    @router.post("/deployment-bundles/exports/{export_id}/reveal")
    def reveal_deployment_bundle_export(export_id: str, request: Request) -> dict[str, Any]:
        _require_local(request)
        try:
            return manager.reveal(export_id)
        except KeyError as exc:
            raise HTTPException(404, "部署包导出任务不存在。") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return manager
