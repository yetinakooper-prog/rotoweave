from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..jobs import JobManager
from ..storage import ObjectStore
from ..workspace_session import WorkspaceRepositoryGateway


@dataclass(slots=True)
class ApiContext:
    """Dependencies used by the current local v4 routes."""

    database: WorkspaceRepositoryGateway
    store: ObjectStore
    jobs: JobManager
    settings: Any
