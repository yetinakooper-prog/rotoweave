from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

from contracts.legacy_compat import compatible_environment_value
from contracts.model_recipe import ASSET_BY_ROLE, ASSETS, RecipeAsset


CHUNK_BYTES = 4 * 1024 * 1024


class DownloadError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, asset: RecipeAsset) -> bool:
    return path.is_file() and path.stat().st_size == asset.bytes and _sha256(path) == asset.sha256


def _copy_response(response: BinaryIO, target: Path, mode: str) -> None:
    with target.open(mode) as output:
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                return
            output.write(chunk)


def download_asset(asset: RecipeAsset, url: str, library: Path) -> dict[str, Any]:
    target = library / asset.filename
    partial_root = library / ".downloads"
    partial = partial_root / f"{asset.filename}.part"
    if target.exists():
        if _verify(target, asset):
            return {"role": asset.role, "file": asset.filename, "bytes": asset.bytes, "sha256": asset.sha256, "reused": True}
        raise DownloadError(f"目标文件已存在但不匹配 Recipe：{asset.filename}")
    library.mkdir(parents=True, exist_ok=True)
    partial_root.mkdir(parents=True, exist_ok=True)
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > asset.bytes:
        partial.unlink()
        offset = 0
    request = urllib.request.Request(url, headers={"User-Agent": "RotoWeave-independent-model-downloader/1"})
    if offset:
        request.add_header("Range", f"bytes={offset}-")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status = int(getattr(response, "status", None) or 200)
            if offset and status != 206:
                offset = 0
                partial.unlink(missing_ok=True)
                request = urllib.request.Request(url, headers={"User-Agent": "RotoWeave-independent-model-downloader/1"})
                with urllib.request.urlopen(request, timeout=120) as restarted:
                    _copy_response(restarted, partial, "wb")
            else:
                _copy_response(response, partial, "ab" if offset else "wb")
    except (OSError, urllib.error.URLError) as exc:
        raise DownloadError(f"下载失败（URL 已隐藏）：{asset.role}: {exc}") from exc
    observed_bytes = partial.stat().st_size
    if observed_bytes != asset.bytes:
        raise DownloadError(f"下载字节数不匹配：{asset.role}: {observed_bytes} != {asset.bytes}")
    observed_sha256 = _sha256(partial)
    if observed_sha256 != asset.sha256:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"下载 SHA-256 不匹配：{asset.role}")
    os.replace(partial, target)
    try:
        partial_root.rmdir()
    except OSError:
        pass
    return {"role": asset.role, "file": asset.filename, "bytes": asset.bytes, "sha256": asset.sha256, "reused": False}


def _sources(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"下载源清单无效：{path}") from exc
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("sources"), dict):
        raise DownloadError("下载源清单必须是 schemaVersion=1 且包含 sources 对象。")
    result = {str(role): str(url).strip() for role, url in payload["sources"].items() if str(url).strip()}
    unknown = set(result) - set(ASSET_BY_ROLE)
    if unknown:
        raise DownloadError(f"下载源包含未知角色：{', '.join(sorted(unknown))}")
    return result


def _default_models_root() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_MODELS_ROOT")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[2] / "RotoWeaveModels"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download independent Recipe model assets with exact verification.")
    parser.add_argument("--sources", type=Path, required=True, help="Local JSON mapping Recipe roles to authorized direct URLs.")
    parser.add_argument("--models-root", type=Path, default=_default_models_root())
    parser.add_argument("--role", action="append", choices=sorted(ASSET_BY_ROLE))
    args = parser.parse_args(argv)
    selected = [ASSET_BY_ROLE[role] for role in args.role] if args.role else list(ASSETS)
    sources = _sources(args.sources.resolve(strict=True))
    missing = [asset.role for asset in selected if asset.role not in sources]
    if missing:
        raise DownloadError(f"下载源缺少角色：{', '.join(missing)}")
    library = args.models_root.expanduser().resolve(strict=False) / "library"
    results = [download_asset(asset, sources[asset.role], library) for asset in selected]
    print(json.dumps({"schemaVersion": 1, "library": str(library), "assets": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DownloadError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
