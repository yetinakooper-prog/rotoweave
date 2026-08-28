from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path


class BrandMigrationError(RuntimeError):
    pass


LEGACY_CLIENT_DIRECTORY = "AIFrameTools-4.0"
LEGACY_SERVER_DIRECTORY = "AIFrameTools-4.0-Server"
CLIENT_DIRECTORY = "RotoWeave-4.0"
SERVER_DIRECTORY = "RotoWeave-4.0-Server"
MIGRATION_RECEIPT = "rotoweave-brand-migration.json"
MODEL_SETTINGS_MIGRATION = "rotoweave-model-settings-migration.json"


def _is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except AttributeError:
        return path.is_symlink()


def _assert_safe_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            candidate = current_path / name
            if _is_reparse(candidate):
                raise BrandMigrationError(
                    f"旧品牌数据包含不允许迁移的链接或目录联接：{candidate}"
                )


def _validate_json_if_present(path: Path) -> None:
    if not path.is_file():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrandMigrationError(f"旧品牌配置无法通过 JSON 校验：{path.name}") from exc
    if not isinstance(value, dict):
        raise BrandMigrationError(f"旧品牌配置必须是 JSON 对象：{path.name}")


def _receipt(source: Path, role: str) -> dict[str, str | int]:
    return {
        "schemaVersion": 1,
        "sourceBrand": "AIFrameTools",
        "sourceVersion": "4.0.0",
        "targetBrand": "RotoWeave",
        "targetVersion": "4.0.0",
        "role": role,
        "sourcePath": str(source),
        "completedAt": datetime.now(timezone.utc).isoformat(),
    }


def _promote(staging: Path, target: Path, source: Path, role: str) -> Path:
    if target.exists():
        raise BrandMigrationError(
            f"RotoWeave 与 AIFrameTools 4.0 数据目录同时存在，禁止自动合并：{target}"
        )
    (staging / MIGRATION_RECEIPT).write_text(
        json.dumps(_receipt(source, role), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    staging.replace(target)
    return target


def _map_predecessor_model_path(value: str) -> str:
    source = Path(value).expanduser().resolve(strict=False)
    parts = list(source.parts)
    try:
        index = next(
            position
            for position, part in enumerate(parts)
            if part.casefold() == "aiframemodels"
        )
    except StopIteration:
        return str(source)
    project_root = Path(__file__).resolve().parents[2]
    suffix = parts[index + 1 :]
    return str(project_root.joinpath("RotoWeaveModels", *suffix).resolve(strict=False))


def _migrate_server_model_settings(source: Path, staging: Path) -> None:
    database = source / "queue.sqlite3"
    if not database.is_file():
        return
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {
                "model_library_roots",
                "model_assets",
                "model_bindings",
            }
            if not required.issubset(tables):
                raise BrandMigrationError("旧服务端模型配置表不完整，拒绝部分迁移。")
            roots = [
                {
                    "label": str(row["label"]),
                    "path": _map_predecessor_model_path(str(row["path"])),
                    "priority": int(row["priority"]),
                    "enabled": bool(row["enabled"]),
                    "readOnly": bool(row["read_only"]),
                }
                for row in connection.execute(
                    "SELECT label,path,priority,enabled,read_only "
                    "FROM model_library_roots ORDER BY priority,path"
                ).fetchall()
            ]
            bindings = {
                str(row["role"]): {
                    "modelId": str(row["model_id"]),
                    "path": _map_predecessor_model_path(str(row["path"])),
                    "bytes": int(row["bytes"]),
                    "sha256": str(row["sha256"]),
                }
                for row in connection.execute(
                    "SELECT b.role,a.model_id,a.path,a.bytes,a.sha256 "
                    "FROM model_bindings b JOIN model_assets a ON a.id=b.asset_id "
                    "ORDER BY b.role"
                ).fetchall()
            }
    except (OSError, sqlite3.Error) as exc:
        raise BrandMigrationError("旧服务端模型配置数据库无法只读迁移。") from exc
    payload = {
        "schemaVersion": 1,
        "sourceBrand": "AIFrameTools",
        "sourceVersion": "4.0.0",
        "targetBrand": "RotoWeave",
        "roots": roots,
        "bindings": bindings,
        "queueMigrated": False,
        "selfTestReceiptsMigrated": False,
    }
    (staging / MODEL_SETTINGS_MIGRATION).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def migrate_client_local_app_data(local_app_data: Path) -> Path:
    base = local_app_data.resolve(strict=True)
    source = base / LEGACY_CLIENT_DIRECTORY
    target = base / CLIENT_DIRECTORY
    if target.exists() or not source.is_dir():
        return target
    _assert_safe_tree(source)
    staging = base / f".{CLIENT_DIRECTORY}.migration-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging, copy_function=shutil.copy2)
        _assert_safe_tree(staging)
        _validate_json_if_present(staging / "client-launcher.json")
        _validate_json_if_present(staging / "recent-workspaces.json")
        return _promote(staging, target, source, "client")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def migrate_server_local_app_data(local_app_data: Path) -> Path:
    base = local_app_data.resolve(strict=True)
    source = base / LEGACY_SERVER_DIRECTORY
    target = base / SERVER_DIRECTORY
    if target.exists() or not source.is_dir():
        return target
    _assert_safe_tree(source)
    staging = base / f".{SERVER_DIRECTORY}.migration-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        launcher = source / "server-launcher.json"
        if launcher.is_file():
            shutil.copy2(launcher, staging / launcher.name)
            _validate_json_if_present(staging / launcher.name)
        _migrate_server_model_settings(source, staging)
        return _promote(staging, target, source, "server")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


__all__ = [
    "BrandMigrationError",
    "CLIENT_DIRECTORY",
    "LEGACY_CLIENT_DIRECTORY",
    "LEGACY_SERVER_DIRECTORY",
    "MIGRATION_RECEIPT",
    "MODEL_SETTINGS_MIGRATION",
    "SERVER_DIRECTORY",
    "migrate_client_local_app_data",
    "migrate_server_local_app_data",
]
