from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar


class LegacyIdentityConflict(RuntimeError):
    """Raised when canonical and predecessor identities disagree."""


def legacy_environment_name(canonical_name: str) -> str | None:
    if canonical_name.startswith("ROTOWEAVE_"):
        return "AIFRAME_" + canonical_name.removeprefix("ROTOWEAVE_")
    return None


def compatible_environment_value(
    canonical_name: str,
    default: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read RotoWeave first and accept one exact AIFrameTools 4.0 fallback.

    Presence is significant: defining both names with different normalized
    values is an identity conflict and never silently selects either side.
    """

    source = os.environ if environ is None else environ
    legacy_name = legacy_environment_name(canonical_name)
    canonical_present = canonical_name in source
    legacy_present = bool(legacy_name and legacy_name in source)
    canonical_value = source.get(canonical_name)
    legacy_value = source.get(legacy_name) if legacy_name else None
    if canonical_present and legacy_present:
        if str(canonical_value).strip() != str(legacy_value).strip():
            raise LegacyIdentityConflict(
                f"环境变量 {canonical_name} 与兼容变量 {legacy_name} 同时存在且值冲突。"
            )
        return canonical_value
    if canonical_present:
        return canonical_value
    if legacy_present:
        return legacy_value
    return default


T = TypeVar("T")


def compatible_header_value(
    headers: Mapping[str, T],
    canonical_name: str,
    legacy_name: str,
) -> T | None:
    """Resolve one canonical/legacy HTTP header pair with conflict rejection."""

    canonical = headers.get(canonical_name)
    legacy = headers.get(legacy_name)
    if canonical is not None and legacy is not None:
        if str(canonical).strip() != str(legacy).strip():
            raise LegacyIdentityConflict(
                f"HTTP 头 {canonical_name} 与兼容头 {legacy_name} 同时存在且值冲突。"
            )
        return canonical
    return canonical if canonical is not None else legacy


__all__ = [
    "LegacyIdentityConflict",
    "compatible_environment_value",
    "compatible_header_value",
    "legacy_environment_name",
]
