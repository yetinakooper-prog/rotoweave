from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from .context import ApiContext
from .presenters import _public_job


def register_job_routes(router: APIRouter, context: ApiContext) -> None:
    database = context.database

    @router.get("/jobs")
    def list_jobs(
        character_id: str | None = None,
        limit: int = Query(100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return [
            _public_job(job)
            for job in database.list_jobs(limit=limit, character_id=character_id)
        ]

    @router.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = context.jobs.cancel(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在。")
        return _public_job(job)

    @router.get("/jobs/events")
    async def job_events(
        request: Request, character_id: str | None = None
    ) -> StreamingResponse:
        # Capture the current repository before the streaming response
        # outlives the request-scoped workspace revision context.
        request_database = (
            database.current() if hasattr(database, "current") else database
        )

        async def stream() -> AsyncIterator[str]:
            last_payload = ""
            while not await request.is_disconnected():
                jobs = await asyncio.to_thread(
                    request_database.list_jobs,
                    limit=200,
                    character_id=character_id,
                )
                snapshot = [_public_job(job) for job in jobs]
                payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                if payload != last_payload:
                    yield f"event: jobs\ndata: {payload}\n\n"
                    last_payload = payload
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")
