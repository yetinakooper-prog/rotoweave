from __future__ import annotations

import unicodedata
import uuid
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalized_display_name(value: str) -> str:
    """Return a stable comparison key while preserving the user's display name."""
    return unicodedata.normalize("NFKC", value).strip().casefold()
