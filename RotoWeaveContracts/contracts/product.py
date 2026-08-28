from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


class ProductContractError(RuntimeError):
    """Raised when a consumer sees a contract other than the only supported v4."""


def _product_file_candidates() -> tuple[Path, ...]:
    source_root = Path(__file__).resolve().parents[1]
    candidates = [source_root / "product.json"]
    if getattr(sys, "frozen", False):
        runtime_root = Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)
        ).resolve()
        candidates.insert(0, runtime_root / "product.json")
    return tuple(candidates)


@lru_cache(maxsize=1)
def load_product_contract() -> dict[str, Any]:
    for candidate in _product_file_candidates():
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductContractError(f"无法读取产品契约：{candidate}") from exc
        runtime = payload.get("runtime")
        contracts = payload.get("contracts")
        if (
            payload.get("product") != "RotoWeave"
            or not isinstance(payload.get("version"), str)
            or not payload["version"].strip()
            or not isinstance(runtime, dict)
            or not isinstance(contracts, dict)
        ):
            raise ProductContractError(f"产品契约结构无效：{candidate}")
        expected = {
            "version": "4.0.0",
            "httpApi": 4,
            "remoteMattingApi": 1,
            "workspaceFormat": 3,
            "runtimeDatabaseSchema": 4,
            "characterPackageFormat": 3,
            "unityScriptedImporter": 8,
        }
        actual = {
            "version": payload["version"],
            **{
                key: contracts.get(key)
                for key in expected
                if key != "version"
            },
        }
        if actual != expected:
            raise ProductContractError(
                f"不兼容的 RotoWeave 4.0 产品契约：期望 {expected}，实际 {actual}"
            )
        return payload
    searched = "、".join(str(path) for path in _product_file_candidates())
    raise ProductContractError(f"缺少 product.json；已检查：{searched}")


def require_contract_version(
    contract: str,
    received: int | str,
    expected: int | str,
) -> None:
    """Reject implicit migration or best-effort parsing at every public boundary."""

    if received != expected:
        raise ProductContractError(
            f"{contract} 版本不兼容：仅支持 {expected}，收到 {received}。"
        )


PRODUCT = load_product_contract()
PRODUCT_VERSION = str(PRODUCT["version"])
RUNTIME = PRODUCT["runtime"]
APPLICATION_DATA_DIRECTORY = str(RUNTIME["applicationDataDirectory"])
DEVELOPMENT_DATA_DIRECTORY = str(RUNTIME["developmentDataDirectory"])
RUNTIME_SINGLE_INSTANCE_MUTEX = str(RUNTIME["singleInstanceMutex"])
SESSION_COOKIE_NAME = str(RUNTIME["sessionCookieName"])
RUNTIME_API_PORT = int(RUNTIME["apiPort"])
RUNTIME_WEB_DEVELOPMENT_PORT = int(RUNTIME["webDevelopmentPort"])
CONTRACTS = PRODUCT["contracts"]
HTTP_API_VERSION = int(CONTRACTS["httpApi"])
HTTP_API_PREFIX = f"/api/v{HTTP_API_VERSION}"
REMOTE_MATTING_API_VERSION = int(CONTRACTS["remoteMattingApi"])
REMOTE_MATTING_API_PREFIX = f"/api/matting/v{REMOTE_MATTING_API_VERSION}"
WORKSPACE_FORMAT_VERSION = int(CONTRACTS["workspaceFormat"])
RUNTIME_DATABASE_SCHEMA_VERSION = int(CONTRACTS["runtimeDatabaseSchema"])
CANONICAL_PIXELS_PER_UNIT = float(CONTRACTS["canonicalPixelsPerUnit"])
COORDINATE_CONTRACT = str(CONTRACTS["coordinateContract"])
CHARACTER_PACKAGE_FORMAT = int(CONTRACTS["characterPackageFormat"])
CHARACTER_PACKAGE_SHAPE = str(CONTRACTS["characterPackageShape"])
UNITY_SCRIPTED_IMPORTER_VERSION = int(CONTRACTS["unityScriptedImporter"])
