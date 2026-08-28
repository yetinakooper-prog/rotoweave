from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Form, HTTPException
from pydantic import ValidationError

from backend import client_launcher

from ..remote_matting_client import (
    RemoteMattingClient,
    RemoteMattingConfig,
    RemoteMattingError,
    RemoteResponseError,
)


def register_service_routes(
    router: APIRouter,
    settings: Any,
) -> None:
    @router.get("/remote-service/settings")
    def get_remote_service_settings() -> dict[str, Any]:
        try:
            return client_launcher.remote_settings()
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_remote_settings", "message": str(exc)},
            ) from exc

    @router.put("/remote-service/settings")
    async def update_remote_service_settings(
        enabled: bool = Form(...),
        host: str = Form(...),
        port: int = Form(...),
    ) -> dict[str, Any]:
        try:
            return client_launcher.save_remote_settings(
                enabled=enabled,
                host=host,
                port=port,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_remote_settings", "message": str(exc)},
            ) from exc

    @router.post("/remote-service/test")
    async def test_remote_service() -> dict[str, Any]:
        service_url = settings.remote_matting_url
        if not service_url:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "remote_service_disabled",
                    "message": "请先启用并保存远程算力服务。",
                },
            )
        try:
            config = RemoteMattingConfig(
                service_url,
                timeout_seconds=8.0,
                max_retries=0,
            )
            async with RemoteMattingClient(config) as client:
                status = await client.probe()
        except RemoteResponseError as exc:
            code = getattr(exc.error.code, "value", str(exc.error.code))
            raise HTTPException(
                status_code=502,
                detail={"code": code, "message": str(exc)},
            ) from exc
        except (httpx.TransportError, RemoteMattingError, ValidationError, ValueError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "remote_service_unreachable",
                    "message": "无法连接可信局域网远程服务，请检查 IPv4、端口和服务端运行状态。",
                },
            ) from exc
        return {"connected": True, **status.model_dump(mode="json")}
