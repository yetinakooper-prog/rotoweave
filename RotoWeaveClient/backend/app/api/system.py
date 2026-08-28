from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import __version__
from ..birefnet import birefnet_health
from contracts.product import HTTP_API_VERSION
from ..schemas import SizeProfileCreate, SizeProfileUpdate
from ..size_system import CANONICAL_PIXELS_PER_UNIT
from .context import ApiContext
from .presenters import _public_size_profile


def register_system_routes(router: APIRouter, context: ApiContext) -> None:
    """Register the current local-only host and Basic-processing surface."""

    database = context.database
    settings = context.settings

    @router.get("/health")
    def health() -> dict[str, Any]:
        accelerator = birefnet_health(settings)
        return {
            "status": "ok",
            "version": __version__,
            "apiVersion": HTTP_API_VERSION,
            "localOnly": True,
            "processing": {
                "basic": {
                    "available": bool(accelerator.get("available")),
                    "mode": accelerator.get("mode"),
                },
                "high": {"owner": "server"},
                "ultra": {"owner": "server"},
            },
        }

    @router.get("/size-system")
    def get_size_system() -> dict[str, Any]:
        profiles = database.list_size_profiles()
        return {
            "canonicalPixelsPerUnit": CANONICAL_PIXELS_PER_UNIT,
            "revisionId": (
                profiles[0]["revisionId"]
                if profiles
                else database.current_target_revision("global:size-profiles")
            ),
            "profiles": [_public_size_profile(item) for item in profiles],
        }

    @router.post("/size-profiles", status_code=201)
    def create_size_profile(payload: SizeProfileCreate) -> dict[str, Any]:
        try:
            return _public_size_profile(
                database.create_size_profile(
                    payload.name,
                    payload.width_world,
                    payload.height_world,
                    payload.unit_mode,
                )
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.patch("/size-profiles/{profile_id}")
    def update_size_profile(
        profile_id: str, payload: SizeProfileUpdate
    ) -> dict[str, Any]:
        try:
            profile = database.update_size_profile(
                profile_id, payload.model_dump(exclude_unset=True)
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not profile:
            raise HTTPException(404, "尺寸档位不存在。")
        return _public_size_profile(profile)

    @router.delete("/size-profiles/{profile_id}", status_code=204)
    def delete_size_profile(profile_id: str) -> None:
        try:
            deleted = database.delete_size_profile(profile_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not deleted:
            raise HTTPException(404, "尺寸档位不存在。")
