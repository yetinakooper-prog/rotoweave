from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import os
from pathlib import Path


MODELS_ROOT_ENV = "ROTOWEAVE_MODELS_ROOT"


def contracts_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_models_root(application_root: Path | None = None) -> Path:
    """Resolve the single current model root; never scan alternate locations."""

    configured = compatible_environment_value(MODELS_ROOT_ENV)
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve(strict=False)
    owner_root = (
        application_root.resolve(strict=False)
        if application_root is not None
        else contracts_root()
    )
    return (owner_root.parent / "RotoWeaveModels").resolve(strict=False)
