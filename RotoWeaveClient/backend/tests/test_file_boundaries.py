from __future__ import annotations

import hashlib
import io
from pathlib import Path

import httpx
import pytest

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.storage import ObjectStore


def _configured_app(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "data",
        runtime_root=tmp_path,
        require_session_token=False,
    )
    app = create_app(settings)
    app.state.workspace_session.create(tmp_path / "workspace", "Test Workspace")
    return settings, app


def test_object_store_limit_and_content_deduplication(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", runtime_root=tmp_path)
    settings.ensure_directories()
    store = ObjectStore(settings)

    with pytest.raises(ValueError, match="不能超过"):
        store.put_stream(io.BytesIO(b"12345"), "too-large.bin", max_bytes=4)
    assert not list((settings.data_root / "temp").glob("*.part"))

    first = store.put_stream(io.BytesIO(b"same"), "same.bin")
    second = store.put_stream(io.BytesIO(b"same"), "same.bin")
    assert first[:3] == second[:3]
    assert first[3] is True
    assert second[3] is False
    assert first[0].read_bytes() == b"same"
    assert not list((settings.data_root / "temp").glob("*.part"))
