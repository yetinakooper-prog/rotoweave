from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..config import SESSION_COOKIE_NAME
from ..network import is_loopback_host


class SessionBootstrap(BaseModel):
    token: str = Field(min_length=16, max_length=256)


def register_session_routes(
    router: APIRouter,
    settings: object,
) -> None:
    def set_session_cookie(
        request: Request,
        response: Response,
    ) -> None:
        if not is_loopback_host(request.client.host if request.client else None):
            raise HTTPException(403, "本机会话只允许 loopback 请求。")
        cookie_value = str(settings.session_token)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=cookie_value,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )

    @router.post("/session/bootstrap", status_code=204)
    def bootstrap_session(
        payload: SessionBootstrap, request: Request, response: Response
    ) -> None:
        if not settings.consume_bootstrap_token(payload.token):
            raise HTTPException(401, "引导令牌无效、已使用或已过期。")
        set_session_cookie(request, response)
