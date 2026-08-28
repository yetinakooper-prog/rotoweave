from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from contracts.model_recipe import RecipeAsset


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "RotoWeaveServer" / "scripts" / "download-independent-models.py"
SPEC = importlib.util.spec_from_file_location("independent_model_download", SCRIPT)
assert SPEC and SPEC.loader
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


def _asset(payload: bytes) -> RecipeAsset:
    return RecipeAsset(
        role="alpha_and_tracking",
        model_id="fixture",
        display_name="Fixture",
        filename="fixture.pt",
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        revision="fixture",
        source_url="file-fixture",
        license_id="test-only",
        profiles=("high",),
        runtime_contract="fixture-v1",
    )


def test_download_independent_asset_verifies_then_reuses(tmp_path: Path) -> None:
    payload = b"independent-model-fixture" * 4096
    source = tmp_path / "source.pt"
    source.write_bytes(payload)
    library = tmp_path / "models" / "library"
    asset = _asset(payload)

    first = download.download_asset(asset, source.as_uri(), library)
    second = download.download_asset(asset, source.as_uri(), library)

    assert first["reused"] is False
    assert second["reused"] is True
    assert (library / asset.filename).read_bytes() == payload
    assert not (library / ".downloads").exists()


def test_download_independent_asset_rejects_hash_mismatch(tmp_path: Path) -> None:
    expected = b"expected"
    source = tmp_path / "source.pt"
    source.write_bytes(b"different")
    asset = _asset(expected)

    with pytest.raises(download.DownloadError, match="字节数不匹配"):
        download.download_asset(asset, source.as_uri(), tmp_path / "models" / "library")
