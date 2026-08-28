from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .api.actions import register_action_routes
from .api.context import ApiContext
from .api.domain_exports import register_domain_export_routes
from .api.deployment_bundles import register_deployment_bundle_routes
from .api.jobs import register_job_routes
from .api.materials import register_material_routes
from .api.session import register_session_routes
from .api.service import register_service_routes
from .api.system import register_system_routes
from .api.workspace import register_workspace_routes
from .jobs import JobManager
from contracts.product import HTTP_API_PREFIX
from .storage import ObjectStore
from .workspace_session import WorkspaceRepositoryGateway, WorkspaceSessionManager


def create_v4_router(
    database: WorkspaceRepositoryGateway,
    store: ObjectStore,
    jobs: JobManager,
    settings: Any,
    session: WorkspaceSessionManager | None = None,
) -> APIRouter:
    """Build the only supported local API router for the 4.0 client."""

    router = APIRouter(prefix=HTTP_API_PREFIX)
    context = ApiContext(database=database, store=store, jobs=jobs, settings=settings)
    register_session_routes(router, settings)
    register_service_routes(router, settings)
    resolved_session = session or getattr(database, "session", None)
    if resolved_session is not None:
        register_workspace_routes(router, resolved_session, jobs)
    register_system_routes(router, context)
    register_material_routes(router, context)
    register_action_routes(router, context)
    register_domain_export_routes(router, context)
    register_deployment_bundle_routes(router)
    register_job_routes(router, context)
    return router
