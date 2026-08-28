from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from ..workspace_format import WorkspaceFormatError
from .context import ApiContext


class ActionCreate(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=128)
    expectedRevisionId: str


class ActionUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    name: str | None = Field(default=None, min_length=1, max_length=128)
    loop: bool | None = None
    previewLoop: bool | None = None
    expectedRevisionId: str

    @model_validator(mode="after")
    def require_change(self) -> "ActionUpdate":
        if self.name is None and self.loop is None and self.previewLoop is None:
            raise ValueError("动作更新至少需要一个字段。")
        return self


class Point(BaseModel):
    model_config = {"extra": "forbid"}
    x: float = 0.0
    y: float = 0.0


class PositiveScale(BaseModel):
    model_config = {"extra": "forbid"}
    x: float = Field(default=1.0, gt=0)
    y: float = Field(default=1.0, gt=0)


class FrameShadow(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    opacity: float | None = Field(default=None, ge=0, le=1)
    offset: Point = Field(default_factory=Point)
    scale: PositiveScale = Field(default_factory=PositiveScale)


class FrameTransform(BaseModel):
    model_config = {"extra": "forbid"}
    position: Point = Field(default_factory=Point)
    scale: PositiveScale = Field(default_factory=PositiveScale)
    rotationDegrees: float = 0.0
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    opacity: float = Field(default=1.0, ge=0, le=1)
    shadow: FrameShadow = Field(default_factory=FrameShadow)


class ActionFrameInput(BaseModel):
    model_config = {"extra": "forbid"}
    id: str | None = Field(default=None, min_length=3, max_length=128)
    variantId: str = Field(min_length=3, max_length=128)
    frameId: str = Field(min_length=3, max_length=128)
    durationSeconds: float = Field(default=1 / 24, gt=0, le=3600)
    enabled: bool = True
    transform: FrameTransform = Field(default_factory=FrameTransform)


class ActionFramesSave(BaseModel):
    model_config = {"extra": "forbid"}
    expectedRevisionId: str
    frames: list[ActionFrameInput] = Field(max_length=100_000)


class ActionFramesAppend(ActionFramesSave):
    frames: list[ActionFrameInput] = Field(min_length=1, max_length=100_000)


def _domain_result(context: ApiContext, **values: Any) -> dict[str, Any]:
    domain = context.database.workspace_domain()
    return {**values, "revisionId": domain["revisionId"]}


def register_action_routes(router: APIRouter, context: ApiContext) -> None:
    database = context.database

    @router.post("/domain/characters/{character_id}/actions", status_code=201)
    def create_action(character_id: str, payload: ActionCreate) -> dict[str, Any]:
        action = database.create_domain_action(
            character_id,
            payload.name,
            expected_revision_id=payload.expectedRevisionId,
        )
        return _domain_result(context, action=action)

    @router.get("/domain/actions/{action_id}")
    def get_action(action_id: str) -> dict[str, Any]:
        action = database.get_domain_action(action_id)
        if action is None:
            raise HTTPException(404, "动作不存在。")
        return _domain_result(context, action=action)

    @router.patch("/domain/actions/{action_id}")
    def update_action(action_id: str, payload: ActionUpdate) -> dict[str, Any]:
        action = database.update_domain_action(
            action_id,
            name=payload.name,
            loop=payload.loop,
            preview_loop=payload.previewLoop,
            expected_revision_id=payload.expectedRevisionId,
        )
        return _domain_result(context, action=action)

    @router.delete("/domain/actions/{action_id}")
    def delete_action(
        action_id: str,
        expected_revision_id: str = Query(..., alias="expectedRevisionId"),
    ) -> dict[str, Any]:
        try:
            removed = database.delete_domain_action(
                action_id,
                expected_revision_id=expected_revision_id,
            )
        except WorkspaceFormatError as exc:
            if str(exc) == "动作不存在。":
                raise HTTPException(404, str(exc)) from exc
            raise
        return _domain_result(context, removed=removed)

    @router.post("/domain/actions/{action_id}/frames")
    def append_frames(action_id: str, payload: ActionFramesAppend) -> dict[str, Any]:
        action = database.append_action_frame_refs(
            action_id,
            [item.model_dump(mode="json", exclude_none=True) for item in payload.frames],
            expected_revision_id=payload.expectedRevisionId,
        )
        return _domain_result(context, action=action)

    @router.put("/domain/actions/{action_id}/frames")
    def save_frames(action_id: str, payload: ActionFramesSave) -> dict[str, Any]:
        action = database.replace_action_frame_refs(
            action_id,
            [item.model_dump(mode="json", exclude_none=True) for item in payload.frames],
            expected_revision_id=payload.expectedRevisionId,
        )
        return _domain_result(context, action=action)

    @router.post("/domain/actions/{action_id}/reset")
    def reset_action_draft(action_id: str) -> dict[str, Any]:
        action = database.get_domain_action(action_id)
        if action is None:
            raise HTTPException(404, "动作不存在。")
        # Drafts live in the React client. Reset therefore returns the last
        # atomically saved action without mutating the workspace revision.
        return _domain_result(context, action=action, reset=True)
