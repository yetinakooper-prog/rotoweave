from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import hashlib
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from contracts.integrity import canonical_sha256
from contracts.brand_migration import MODEL_SETTINGS_MIGRATION
from contracts.hardware import probe_cuda_hardware
from contracts.model_recipe import (
    ASSET_BY_ROLE,
    ASSET_BY_SHA256,
    ASSETS,
    MODEL_RECIPE_ID,
    PROFILE_ROLES,
    RECIPE,
    RECIPE_DIGEST,
)
from contracts.model_compatibility import (
    MODEL_COMPATIBILITY_POLICY,
    MODEL_COMPATIBILITY_POLICY_DIGEST,
    profile_configuration_digest,
    role_accepts_extension,
)
from contracts.model_runtime_recipe import runtime_recipe
from contracts.paths import resolve_models_root

from .repository import RemoteQueueRepository, utc_now
from .native_model_picker import ModelPicker, WindowsNativeModelPicker


ProfileTester = Callable[[str, dict[str, Any], threading.Event], dict[str, Any]]
AssetInspector = Callable[[str, Path, threading.Event], dict[str, Any]]
ConfigurationActivator = Callable[[dict[str, Any]], dict[str, Any]]
SelfTestBegin = Callable[[threading.Event], None]
SelfTestEnd = Callable[[], None]


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return True
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _safe_root(value: str | Path) -> Path:
    original = Path(value)
    if not original.is_absolute():
        raise ValueError("模型库必须是现存的服务器本机绝对目录。")
    path = original.expanduser()
    if not path.is_dir():
        raise ValueError("模型库必须是现存的服务器本机绝对目录。")
    resolved = path.resolve(strict=True)
    if resolved == Path(resolved.anchor) or resolved.is_symlink() or _is_reparse_point(resolved):
        raise ValueError("模型库不能是磁盘根目录、符号链接或重解析点。")
    return resolved


def _regular_files(root: Path):
    """Walk without following symlinks or Windows reparse-point directories."""

    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse_point(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path
            except OSError:
                continue


def _safe_candidate(root: Path, path: Path) -> Path:
    try:
        lexical = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("模型候选解析前已逃逸模型库根目录。") from exc
    current = root
    for part in lexical.parts:
        current = current / part
        if current.is_symlink() or _is_reparse_point(current):
            raise ValueError("模型候选及其父目录不能是符号链接或重解析点。")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("模型候选解析后逃逸模型库根目录。") from exc
    if not resolved.is_file():
        raise ValueError("模型候选必须是普通文件。")
    return resolved


def _sha256_candidate(path: Path, cancel: threading.Event) -> str | None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            if cancel.is_set():
                return None
            digest.update(chunk)
    return digest.hexdigest()


_GPU_CACHE: tuple[float, str | None, dict[str, str] | None] = (0.0, None, None)
_GPU_CACHE_LOCK = threading.Lock()


def _current_gpu(preferred_uuid: str | None = None) -> dict[str, str] | None:
    """Return a short-lived UUID/driver snapshot for receipt invalidation."""

    global _GPU_CACHE
    now = time.monotonic()
    with _GPU_CACHE_LOCK:
        if now - _GPU_CACHE[0] < 10.0 and _GPU_CACHE[1] == preferred_uuid:
            return dict(_GPU_CACHE[2]) if _GPU_CACHE[2] else None
        selected = probe_cuda_hardware(preferred_uuid).selected
        result = (
            {
                "uuid": selected.uuid,
                "name": selected.name,
                "driverVersion": selected.driver_version,
            }
            if selected is not None
            else None
        )
        _GPU_CACHE = (now, preferred_uuid, result)
        return dict(result) if result else None


class ModelCenter:
    """Recipe-owned, path-only model management for the localhost admin."""

    def __init__(
        self,
        repository: RemoteQueueRepository,
        runtime_root: Path,
        *,
        picker: ModelPicker | None = None,
    ):
        self.repository = repository
        self.runtime_root = runtime_root.resolve(strict=False)
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._guard = threading.RLock()
        self._dialog_guard = threading.Lock()
        self._picker = picker or WindowsNativeModelPicker()
        self._profile_tester: ProfileTester | None = None
        self._asset_inspector: AssetInspector | None = None
        self._activator: ConfigurationActivator | None = None
        self._self_test_begin: SelfTestBegin | None = None
        self._self_test_end: SelfTestEnd | None = None
        self._cleanup_single_active_state()
        self.default_root_id, self.default_scan_required = self._ensure_default_models_root()
        self.migration_scan_required = False

    def _cleanup_single_active_state(self) -> None:
        """Migrate persisted model state to one active config and current bindings."""

        with self.repository.transaction() as connection:
            active = connection.execute(
                "SELECT digest FROM model_configurations WHERE active=1 "
                "ORDER BY activated_at DESC,created_at DESC LIMIT 1"
            ).fetchone()
            if active is None:
                connection.execute("DELETE FROM model_configurations")
            else:
                connection.execute(
                    "DELETE FROM model_configurations WHERE digest<>?",
                    (str(active["digest"]),),
                )
            connection.execute(
                "DELETE FROM model_assets WHERE id NOT IN (SELECT asset_id FROM model_bindings) "
                "AND id NOT IN (SELECT asset_id FROM model_configuration_assets)"
            )
            connection.execute(
                "DELETE FROM model_library_roots WHERE read_only=0 "
                "AND id NOT IN (SELECT DISTINCT root_id FROM model_assets)"
            )

    def choose_folder(self) -> Path | None:
        if not self._dialog_guard.acquire(blocking=False):
            raise ValueError("已有模型文件选择对话框正在打开。")
        try:
            return self._picker.choose_folder()
        finally:
            self._dialog_guard.release()

    def choose_file(self, role: str) -> Path | None:
        recipe = ASSET_BY_ROLE.get(role)
        if recipe is None:
            raise KeyError(role)
        if not self._dialog_guard.acquire(blocking=False):
            raise ValueError("已有模型文件选择对话框正在打开。")
        try:
            return self._picker.choose_file(recipe.display_name)
        finally:
            self._dialog_guard.release()

    @staticmethod
    def _selection_root_id(path: Path) -> str:
        return "root-selection-" + hashlib.sha256(
            str(path).casefold().encode("utf-8")
        ).hexdigest()[:20]

    def _upsert_selection_root(self, connection: Any, path: Path, *, read_only: bool = False) -> str:
        root_id = self._selection_root_id(path)
        now = utc_now()
        existing = connection.execute(
            "SELECT id FROM model_library_roots WHERE path=?", (str(path),)
        ).fetchone()
        if existing is not None:
            root_id = str(existing["id"])
            connection.execute(
                "UPDATE model_library_roots SET label=?,enabled=1,updated_at=? WHERE id=?",
                (path.name, now, root_id),
            )
            return root_id
        connection.execute(
            "INSERT INTO model_library_roots(id,label,path,priority,enabled,read_only,created_at,updated_at) "
            "VALUES(?,?,?,0,1,?,?,?)",
            (root_id, path.name, str(path), 1 if read_only else 0, now, now),
        )
        return root_id

    def _upsert_selected_asset(
        self,
        connection: Any,
        *,
        root_id: str,
        role: str,
        path: Path,
        size: int,
        digest: str,
    ) -> str:
        recipe = ASSET_BY_ROLE[role]
        existing = connection.execute(
            "SELECT id,role FROM model_assets WHERE path=?", (str(path),)
        ).fetchone()
        asset_id = (
            str(existing["id"])
            if existing is not None
            else "asset-" + hashlib.sha256(f"{role}\0{path}".encode("utf-8")).hexdigest()[:24]
        )
        if existing is not None and str(existing["role"]) != role:
            connection.execute("DELETE FROM model_bindings WHERE asset_id=?", (asset_id,))
        official = size == recipe.bytes and digest == recipe.sha256
        message = (
            "官方精确身份已发现；仍需执行显式验证与 Profile 自检。"
            if official
            else "身份未知，需执行安全结构验证。"
        )
        now = utc_now()
        connection.execute(
            "INSERT INTO model_assets(id,root_id,role,model_id,path,bytes,sha256,state,error_text,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'candidate',?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "root_id=excluded.root_id,role=excluded.role,model_id=excluded.model_id,bytes=excluded.bytes,"
            "sha256=excluded.sha256,state='candidate',verification_kind=NULL,verification_contract_digest=NULL,"
            "verification_receipt_digest=NULL,error_text=excluded.error_text,verified_at=NULL,updated_at=excluded.updated_at",
            (asset_id, root_id, role, recipe.model_id, str(path), size, digest, message, now, now),
        )
        connection.execute(
            "INSERT INTO model_bindings(role,asset_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(role) DO UPDATE SET asset_id=excluded.asset_id,updated_at=excluded.updated_at",
            (role, asset_id, now),
        )
        return asset_id

    @staticmethod
    def _stable_hash(path: Path, cancel: threading.Event) -> tuple[int, str] | None:
        before = path.stat()
        digest = _sha256_candidate(path, cancel)
        if digest is None:
            return None
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"扫描期间模型文件发生变化：{path.name}")
        return int(after.st_size), digest

    def select_folder(self, selected_path: Path) -> dict[str, Any]:
        def execute(operation_id: str, cancel: threading.Event) -> None:
            root = _safe_root(selected_path)
            expected_sizes = {item.bytes for item in ASSETS}
            name_roles: dict[str, list[str]] = {}
            for item in ASSETS:
                name_roles.setdefault(item.filename.casefold(), []).append(item.role)
            extensions = {".pt", ".pth", ".safetensors", ".ckpt", ".bin"}
            scanned: dict[str, list[tuple[Path, int, str]]] = {item.role: [] for item in ASSETS}
            files = [
                path for path in _regular_files(root)
                if path.suffix.casefold() in extensions
                and (path.name.casefold() in name_roles or path.stat().st_size in expected_sizes)
            ]
            total = max(1, len(files))
            for index, raw_path in enumerate(files):
                if cancel.is_set():
                    return
                path = _safe_candidate(root, raw_path)
                hashed = self._stable_hash(path, cancel)
                if hashed is None:
                    return
                size, digest = hashed
                exact = ASSET_BY_SHA256.get(digest)
                roles = (
                    [exact.role]
                    if exact is not None and size == exact.bytes and role_accepts_extension(exact.role, path.suffix)
                    else [
                        role for role in name_roles.get(path.name.casefold(), [])
                        if role_accepts_extension(role, path.suffix)
                    ]
                )
                if len(roles) == 1:
                    scanned[roles[0]].append((path, size, digest))
                self._update_operation(
                    operation_id,
                    stage="hashing",
                    progress=(index + 1) / total,
                    detail={"candidateCount": len(files)},
                )
            ambiguous_roles = sorted(role for role, values in scanned.items() if len(values) > 1)
            missing_roles = sorted(role for role, values in scanned.items() if not values)
            unique = {role: values[0] for role, values in scanned.items() if len(values) == 1}
            updated_roles: list[str] = []
            with self.repository.transaction() as connection:
                root_id = self._upsert_selection_root(connection, root)
                for role, (path, size, digest) in sorted(unique.items()):
                    self._upsert_selected_asset(
                        connection,
                        root_id=root_id,
                        role=role,
                        path=path,
                        size=size,
                        digest=digest,
                    )
                    updated_roles.append(role)
                connection.execute(
                    "DELETE FROM model_assets WHERE id NOT IN (SELECT asset_id FROM model_bindings) "
                    "AND id NOT IN (SELECT asset_id FROM model_configuration_assets)"
                )
                connection.execute(
                    "DELETE FROM model_library_roots WHERE read_only=0 "
                    "AND id NOT IN (SELECT DISTINCT root_id FROM model_assets)"
                )
            self._update_operation(
                operation_id,
                stage="selected",
                progress=1.0,
                detail={
                    "selectedFolder": str(root),
                    "updatedRoles": updated_roles,
                    "ambiguousRoles": ambiguous_roles,
                    "missingRoles": missing_roles,
                },
            )

        return self._start("select_folder", execute)

    def select_file(self, role: str, selected_path: Path) -> dict[str, Any]:
        if role not in ASSET_BY_ROLE:
            raise KeyError(role)

        def execute(operation_id: str, cancel: threading.Event) -> None:
            root = _safe_root(selected_path.parent)
            path = _safe_candidate(root, selected_path)
            if not role_accepts_extension(role, path.suffix):
                raise ValueError("该槽位不接受此模型容器类型。")
            hashed = self._stable_hash(path, cancel)
            if hashed is None:
                return
            size, digest = hashed
            with self.repository.transaction() as connection:
                root_id = self._upsert_selection_root(connection, root)
                self._upsert_selected_asset(
                    connection,
                    root_id=root_id,
                    role=role,
                    path=path,
                    size=size,
                    digest=digest,
                )
                connection.execute(
                    "DELETE FROM model_assets WHERE id NOT IN (SELECT asset_id FROM model_bindings) "
                    "AND id NOT IN (SELECT asset_id FROM model_configuration_assets)"
                )
            self._update_operation(
                operation_id,
                stage="selected",
                progress=1.0,
                detail={"role": role, "selectedFile": str(path)},
            )

        return self._start("select_file", execute)

    def select_default(self) -> dict[str, Any]:
        models_root = resolve_models_root(self.runtime_root) / "library"
        return self.select_folder(models_root)

    @property
    def _model_migration_path(self) -> Path:
        return self.repository.path.parent / MODEL_SETTINGS_MIGRATION

    @property
    def _model_migration_receipt_path(self) -> Path:
        return self.repository.path.parent / "rotoweave-model-settings-applied.json"

    def _model_migration_payload(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._model_migration_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("品牌迁移生成的模型设置无法读取。") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != 1
            or payload.get("targetBrand") != "RotoWeave"
            or payload.get("queueMigrated") is not False
            or payload.get("selfTestReceiptsMigrated") is not False
            or not isinstance(payload.get("roots"), list)
            or not isinstance(payload.get("bindings"), dict)
        ):
            raise RuntimeError("品牌迁移生成的模型设置不符合受控契约。")
        return payload

    def _import_migrated_model_roots(self) -> bool:
        payload = self._model_migration_payload()
        if payload is None:
            return False
        imported = False
        with self.repository.transaction() as connection:
            existing_paths = {
                str(row["path"]).casefold()
                for row in connection.execute("SELECT path FROM model_library_roots").fetchall()
            }
            for root in payload["roots"]:
                if not isinstance(root, dict) or not bool(root.get("enabled")):
                    continue
                try:
                    path = _safe_root(str(root.get("path") or ""))
                except (OSError, ValueError):
                    continue
                if str(path).casefold() in existing_paths:
                    continue
                root_id = "root-migrated-" + hashlib.sha256(
                    str(path).casefold().encode("utf-8")
                ).hexdigest()[:16]
                now = utc_now()
                connection.execute(
                    "INSERT INTO model_library_roots(id,label,path,priority,enabled,read_only,created_at,updated_at) "
                    "VALUES(?,?,?,?,1,0,?,?)",
                    (
                        root_id,
                        str(root.get("label") or path.name).strip() or path.name,
                        str(path),
                        int(root.get("priority") or 0),
                        now,
                        now,
                    ),
                )
                existing_paths.add(str(path).casefold())
                imported = True
        try:
            receipt = json.loads(self._model_migration_receipt_path.read_text(encoding="utf-8"))
            applied = {str(role) for role in receipt.get("appliedRoles", [])}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            applied = set()
        return imported or bool(set(payload["bindings"]) - applied)

    def _apply_migrated_model_bindings(self) -> None:
        payload = self._model_migration_payload()
        if payload is None:
            return
        try:
            receipt = json.loads(self._model_migration_receipt_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            receipt = {"schemaVersion": 1, "appliedRoles": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("模型设置迁移回执无法读取。") from exc
        applied = {
            str(role) for role in receipt.get("appliedRoles", []) if str(role) in ASSET_BY_ROLE
        }
        with self.repository.transaction() as connection:
            for role, expected in payload["bindings"].items():
                if role in applied or role not in ASSET_BY_ROLE or not isinstance(expected, dict):
                    continue
                row = connection.execute(
                    "SELECT id FROM model_assets WHERE role=? AND sha256=? "
                    "AND state IN ('verified','candidate','incompatible') "
                    "ORDER BY CASE state WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,path LIMIT 1",
                    (role, str(expected.get("sha256") or "")),
                ).fetchone()
                if row is None:
                    continue
                connection.execute(
                    "INSERT INTO model_bindings(role,asset_id,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(role) DO UPDATE SET asset_id=excluded.asset_id,updated_at=excluded.updated_at",
                    (role, str(row["id"]), utc_now()),
                )
                applied.add(role)
        pending = sorted(set(payload["bindings"]) - applied)
        result = {
            "schemaVersion": 1,
            "targetBrand": "RotoWeave",
            "appliedRoles": sorted(applied),
            "pendingRoles": pending,
            "selfTestRequired": True,
            "updatedAt": utc_now(),
        }
        temporary = self._model_migration_receipt_path.with_suffix(
            f".partial-{uuid.uuid4().hex}"
        )
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(self._model_migration_receipt_path)

    def set_profile_tester(self, callback: ProfileTester) -> None:
        self._profile_tester = callback

    def set_asset_inspector(self, callback: AssetInspector) -> None:
        self._asset_inspector = callback

    def set_activator(self, callback: ConfigurationActivator) -> None:
        self._activator = callback

    def set_self_test_lifecycle(self, begin: SelfTestBegin, end: SelfTestEnd) -> None:
        self._self_test_begin = begin
        self._self_test_end = end

    def runtime_descriptor(self, profile: str) -> dict[str, Any]:
        contract = runtime_recipe(profile)
        env_name = f"ROTOWEAVE_{profile.upper()}_RUNTIME"
        default = self.runtime_root / "server-runtimes" / profile / str(contract["pythonRelativePath"])
        configured = compatible_environment_value(env_name)
        executable = (Path(configured).expanduser() if configured else default).resolve(strict=False)
        source = "missing"
        installed = executable.is_file()
        observed_digest = str(contract["digest"])
        if installed:
            manifest_override = compatible_environment_value(f"ROTOWEAVE_{profile.upper()}_RUNTIME_MANIFEST")
            manifest_path = (
                Path(manifest_override).expanduser().resolve(strict=False)
                if manifest_override
                else self.runtime_root / "server-runtimes" / profile / "runtime-manifest.json"
            )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_digest = str(manifest.get("digest") or "")
                identity = dict(manifest)
                identity.pop("digest", None)
                if manifest_digest != canonical_sha256(identity) or manifest_digest != contract["digest"]:
                    raise ValueError("runtime manifest digest mismatch")
                source = "product"
            except (OSError, ValueError, json.JSONDecodeError):
                installed = False
                source = "invalid-product-manifest"
                observed_digest = canonical_sha256({"profile": profile, "state": source})
        payload = {
            **contract,
            "python": str(executable),
            "installed": installed,
            "source": source,
            "digest": observed_digest,
        }
        return payload

    def _ensure_default_models_root(self) -> tuple[str | None, bool]:
        models_root = resolve_models_root(self.runtime_root) / "library"
        if not models_root.is_dir():
            return None, False
        resolved = _safe_root(models_root)
        root_id = "root-default-rotoweave-models"
        now = utc_now()
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO model_library_roots(id,label,path,priority,enabled,read_only,created_at,updated_at) "
                "VALUES(?,?,?,1,1,1,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,enabled=1,updated_at=excluded.updated_at",
                (root_id, "RotoWeaveModels 默认独立模型库", str(resolved), now, now),
            )
            stale_asset_ids: list[str] = []
            for asset in connection.execute(
                "SELECT id,path FROM model_assets WHERE root_id=? AND state<>'retired'",
                (root_id,),
            ).fetchall():
                candidate = Path(str(asset["path"])).resolve(strict=False)
                try:
                    candidate.relative_to(resolved)
                except ValueError:
                    stale_asset_ids.append(str(asset["id"]))
            if stale_asset_ids:
                placeholders = ",".join("?" for _ in stale_asset_ids)
                connection.execute(
                    f"DELETE FROM model_bindings WHERE asset_id IN ({placeholders})",
                    stale_asset_ids,
                )
                connection.execute(
                    f"UPDATE model_assets SET state='retired',error_text='默认独立模型根已变更',updated_at=? "
                    f"WHERE id IN ({placeholders})",
                    (now, *stale_asset_ids),
                )
                connection.execute(
                    f"UPDATE model_configurations SET state='retired',active=0 WHERE digest IN ("
                    f"SELECT DISTINCT configuration_digest FROM model_configuration_assets "
                    f"WHERE asset_id IN ({placeholders}))",
                    stale_asset_ids,
                )
            asset_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM model_assets WHERE root_id=? AND state='verified'",
                    (root_id,),
                ).fetchone()["value"]
            )
        return root_id, asset_count == 0

    def add_root(self, path: str, label: str | None = None) -> dict[str, Any]:
        resolved = _safe_root(path)
        now = utc_now()
        root_id = f"root-{uuid.uuid4().hex}"
        with self.repository.transaction() as connection:
            priority = int(connection.execute("SELECT COALESCE(MAX(priority),-1)+1 AS value FROM model_library_roots").fetchone()["value"])
            connection.execute(
                "INSERT INTO model_library_roots(id,label,path,priority,enabled,read_only,created_at,updated_at) VALUES(?,?,?,?,1,0,?,?)",
                (root_id, (label or resolved.name).strip() or resolved.name, str(resolved), priority, now, now),
            )
        return self.root(root_id)

    def root(self, root_id: str) -> dict[str, Any]:
        with self.repository.connect() as connection:
            row = connection.execute("SELECT * FROM model_library_roots WHERE id=?", (root_id,)).fetchone()
        if row is None:
            raise KeyError(root_id)
        return self._root_dto(_row(row))

    @staticmethod
    def _root_dto(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "label": value["label"],
            "path": value["path"],
            "priority": int(value["priority"]),
            "enabled": bool(value["enabled"]),
            "readOnly": bool(value["read_only"]),
            "createdAt": value["created_at"],
            "updatedAt": value["updated_at"],
        }

    def update_root(self, root_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = self.root(root_id)
        if current["readOnly"] and any(key in body for key in ("path", "enabled")):
            raise ValueError("系统只读模型根目录不可修改。")
        path = _safe_root(str(body["path"])) if "path" in body else Path(current["path"])
        label = str(body.get("label", current["label"])).strip() or Path(path).name
        enabled = 1 if bool(body.get("enabled", current["enabled"])) else 0
        priority = int(body.get("priority", current["priority"]))
        with self.repository.transaction() as connection:
            connection.execute(
                "UPDATE model_library_roots SET label=?,path=?,enabled=?,priority=?,updated_at=? WHERE id=?",
                (label, str(path), enabled, priority, utc_now(), root_id),
            )
        return self.root(root_id)

    def delete_root(self, root_id: str) -> None:
        current = self.root(root_id)
        if current["readOnly"]:
            raise ValueError("系统只读模型根目录不可删除。")
        with self.repository.transaction() as connection:
            references = int(connection.execute(
                "SELECT COUNT(*) AS value FROM model_bindings b JOIN model_assets a ON a.id=b.asset_id WHERE a.root_id=?",
                (root_id,),
            ).fetchone()["value"])
            if references:
                raise ValueError("模型库仍有草稿绑定，必须先解除绑定。")
            connection.execute("DELETE FROM model_library_roots WHERE id=?", (root_id,))

    def _operation(self, operation_id: str) -> dict[str, Any]:
        with self.repository.connect() as connection:
            row = connection.execute("SELECT * FROM model_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(operation_id)
        value = _row(row)
        return {
            "id": value["id"],
            "kind": value["kind"],
            "state": value["state"],
            "stage": value["stage"],
            "progress": float(value["progress"]),
            "cancelRequested": bool(value["cancel_requested"]),
            "detail": json.loads(value["detail_json"] or "{}"),
            "error": value["error_text"],
            "createdAt": value["created_at"],
            "updatedAt": value["updated_at"],
        }

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._operation(operation_id)

    def operations(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.repository.connect() as connection:
            rows = connection.execute("SELECT id FROM model_operations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._operation(str(item["id"])) for item in rows]

    def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if operation["kind"] == "activate" and operation["stage"] in {"draining", "switching"}:
            raise ValueError("配置切换进入排空后不可取消。")
        event = self._cancel.get(operation_id)
        if event is not None:
            event.set()
        with self.repository.transaction() as connection:
            connection.execute("UPDATE model_operations SET cancel_requested=1,updated_at=? WHERE id=?", (utc_now(), operation_id))
        return self._operation(operation_id)

    def _start(self, kind: str, target: Callable[[str, threading.Event], None]) -> dict[str, Any]:
        with self.repository.connect() as connection:
            running = connection.execute(
                "SELECT id FROM model_operations WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone()
        if running is not None:
            raise ValueError("已有模型操作正在执行，请等待完成或取消。")
        operation_id = f"model-op-{uuid.uuid4().hex}"
        now = utc_now()
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO model_operations(id,kind,state,stage,progress,detail_json,created_at,updated_at) VALUES(?,?,'queued','queued',0,'{}',?,?)",
                (operation_id, kind, now, now),
            )
        cancel = threading.Event()
        self._cancel[operation_id] = cancel

        def run() -> None:
            self._update_operation(operation_id, state="running", stage=kind, progress=0.01)
            try:
                target(operation_id, cancel)
                if cancel.is_set():
                    self._update_operation(operation_id, state="cancelled", stage="cancelled")
                else:
                    self._update_operation(operation_id, state="passed", stage="passed", progress=1.0)
            except Exception as exc:
                if cancel.is_set():
                    self._update_operation(operation_id, state="cancelled", stage="cancelled", error=None)
                else:
                    self._update_operation(operation_id, state="failed", stage="failed", error=str(exc))
                    self.repository.log(None, "error", f"model.{kind}_failed", {"operationId": operation_id, "message": str(exc)}, component="model")
            finally:
                self._cancel.pop(operation_id, None)
                self._threads.pop(operation_id, None)

        thread = threading.Thread(target=run, name=operation_id, daemon=True)
        self._threads[operation_id] = thread
        thread.start()
        return self._operation(operation_id)

    def _update_operation(
        self,
        operation_id: str,
        *,
        state: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        current = self._operation(operation_id)
        with self.repository.transaction() as connection:
            connection.execute(
                "UPDATE model_operations SET state=?,stage=?,progress=?,detail_json=?,error_text=?,updated_at=? WHERE id=?",
                (
                    state or current["state"],
                    stage or current["stage"],
                    max(0.0, min(1.0, current["progress"] if progress is None else progress)),
                    json.dumps(detail if detail is not None else current["detail"], ensure_ascii=False, sort_keys=True),
                    error,
                    utc_now(),
                    operation_id,
                ),
            )

    def scan(self, root_ids: list[str] | None = None) -> dict[str, Any]:
        selected = set(root_ids or [])

        def execute(operation_id: str, cancel: threading.Event) -> None:
            with self.repository.connect() as connection:
                rows = connection.execute("SELECT * FROM model_library_roots WHERE enabled=1 ORDER BY priority,path").fetchall()
            roots = [_row(item) for item in rows if not selected or str(item["id"]) in selected]
            if not roots:
                raise ValueError("没有可扫描的模型库根目录。")
            expected_sizes = {item.bytes for item in ASSETS}
            canonical_names = {item.filename.casefold() for item in ASSETS}
            extensions = {".pt", ".pth", ".safetensors", ".ckpt", ".bin"}
            candidates: list[tuple[dict[str, Any], Path]] = []
            for root in roots:
                root_path = _safe_root(str(root["path"]))
                with self.repository.connect() as connection:
                    known = connection.execute("SELECT id,path FROM model_assets WHERE root_id=?", (root["id"],)).fetchall()
                for record in known:
                    try:
                        _safe_candidate(root_path, Path(str(record["path"])))
                    except (OSError, ValueError) as exc:
                        with self.repository.transaction() as connection:
                            connection.execute(
                                "UPDATE model_assets SET state='missing',error_text=?,updated_at=? WHERE id=?",
                                (str(exc), utc_now(), record["id"]),
                            )
                for path in _regular_files(root_path):
                    if cancel.is_set():
                        return
                    try:
                        if (
                            path.suffix.casefold() in extensions
                            and path.is_file()
                            and (
                                path.stat().st_size in expected_sizes
                                or path.name.casefold() in canonical_names
                            )
                        ):
                            candidates.append((root, _safe_candidate(root_path, path)))
                    except (OSError, ValueError):
                        continue
            total = max(1, len(candidates))
            found: list[str] = []
            for index, (root, path) in enumerate(candidates):
                if cancel.is_set():
                    return
                digest = _sha256_candidate(path, cancel)
                if digest is None:
                    return
                recipe = ASSET_BY_SHA256.get(digest)
                if recipe is not None and path.stat().st_size == recipe.bytes:
                    asset_id = "asset-" + hashlib.sha256(f"{recipe.role}\0{path}".encode("utf-8")).hexdigest()[:24]
                    now = utc_now()
                    with self.repository.transaction() as connection:
                        connection.execute(
                            "INSERT INTO model_assets(id,root_id,role,model_id,path,bytes,sha256,state,verification_kind,verification_contract_digest,error_text,verified_at,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,'verified','official',?,NULL,?,?,?) ON CONFLICT(path) DO UPDATE SET root_id=excluded.root_id,role=excluded.role,model_id=excluded.model_id,bytes=excluded.bytes,sha256=excluded.sha256,state='verified',verification_kind='official',verification_contract_digest=excluded.verification_contract_digest,verification_receipt_digest=NULL,error_text=NULL,verified_at=excluded.verified_at,updated_at=excluded.updated_at",
                            (asset_id, root["id"], recipe.role, recipe.model_id, str(path), recipe.bytes, digest, RECIPE_DIGEST, now, now, now),
                        )
                        current = connection.execute(
                            "SELECT a.state FROM model_bindings b JOIN model_assets a ON a.id=b.asset_id WHERE b.role=?",
                            (recipe.role,),
                        ).fetchone()
                        if current is None or str(current["state"]) != "verified":
                            connection.execute(
                                "INSERT INTO model_bindings(role,asset_id,updated_at) VALUES(?,?,?) "
                                "ON CONFLICT(role) DO UPDATE SET asset_id=excluded.asset_id,updated_at=excluded.updated_at",
                                (recipe.role, asset_id, now),
                            )
                    found.append(recipe.role)
                else:
                    recipe = next(
                        (item for item in ASSETS if item.filename.casefold() == path.name.casefold()),
                        None,
                    )
                    if recipe is not None and role_accepts_extension(recipe.role, path.suffix):
                        asset_id = "asset-" + hashlib.sha256(f"{recipe.role}\0{path}".encode("utf-8")).hexdigest()[:24]
                        now = utc_now()
                        message = "文件名命中 Recipe 槽位但身份未知，需执行安全结构验证。"
                        with self.repository.transaction() as connection:
                            connection.execute(
                                "INSERT INTO model_assets(id,root_id,role,model_id,path,bytes,sha256,state,error_text,created_at,updated_at) "
                                "VALUES(?,?,?,?,?,?,?,'candidate',?,?,?) ON CONFLICT(path) DO UPDATE SET root_id=excluded.root_id,role=excluded.role,model_id=excluded.model_id,bytes=excluded.bytes,sha256=excluded.sha256,state='candidate',verification_kind=NULL,verification_contract_digest=NULL,verification_receipt_digest=NULL,error_text=excluded.error_text,verified_at=NULL,updated_at=excluded.updated_at",
                                (asset_id, root["id"], recipe.role, recipe.model_id, str(path), path.stat().st_size, digest, message, now, now),
                            )
                self._update_operation(operation_id, stage="hashing", progress=(index + 1) / total, detail={"candidateCount": len(candidates), "matchedRoles": sorted(set(found))})
            self._apply_migrated_model_bindings()

        return self._start("scan", execute)

    def register_candidate(self, role: str, root_id: str, relative_path: str) -> dict[str, Any]:
        if role not in ASSET_BY_ROLE:
            raise KeyError(role)
        relative = Path(relative_path)
        if not relative_path.strip() or relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ValueError("兼容候选必须使用已启用模型根内的相对路径。")
        with self.repository.connect() as connection:
            root_row = connection.execute(
                "SELECT * FROM model_library_roots WHERE id=? AND enabled=1",
                (root_id,),
            ).fetchone()
        if root_row is None:
            raise ValueError("模型根不存在或未启用。")
        root = _safe_root(str(root_row["path"]))
        candidate = _safe_candidate(root, root / relative)
        if not role_accepts_extension(role, candidate.suffix):
            raise ValueError("该槽位不接受此模型容器类型。")
        digest = _sha256_candidate(candidate, threading.Event())
        if digest is None:
            raise RuntimeError("候选哈希计算被取消。")
        recipe = ASSET_BY_ROLE[role]
        is_official = candidate.stat().st_size == recipe.bytes and digest == recipe.sha256
        asset_id = "asset-" + hashlib.sha256(f"{role}\0{candidate}".encode("utf-8")).hexdigest()[:24]
        now = utc_now()
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO model_assets(id,root_id,role,model_id,path,bytes,sha256,state,verification_kind,verification_contract_digest,error_text,verified_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET root_id=excluded.root_id,role=excluded.role,model_id=excluded.model_id,bytes=excluded.bytes,sha256=excluded.sha256,state=excluded.state,verification_kind=excluded.verification_kind,verification_contract_digest=excluded.verification_contract_digest,verification_receipt_digest=NULL,error_text=excluded.error_text,verified_at=excluded.verified_at,updated_at=excluded.updated_at",
                (
                    asset_id,
                    root_id,
                    role,
                    recipe.model_id,
                    str(candidate),
                    candidate.stat().st_size,
                    digest,
                    "verified" if is_official else "candidate",
                    "official" if is_official else None,
                    RECIPE_DIGEST if is_official else None,
                    None if is_official else "身份未知，需执行安全结构验证。",
                    now if is_official else None,
                    now,
                    now,
                ),
            )
        return self._asset_dto(self._asset(asset_id))

    def _asset(self, asset_id: str) -> dict[str, Any]:
        with self.repository.connect() as connection:
            row = connection.execute("SELECT * FROM model_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        return _row(row)

    def bind(self, role: str, asset_id: str) -> dict[str, Any]:
        if role not in ASSET_BY_ROLE:
            raise KeyError(role)
        with self.repository.transaction() as connection:
            asset = connection.execute("SELECT * FROM model_assets WHERE id=?", (asset_id,)).fetchone()
            if asset is None or str(asset["role"]) != role:
                raise ValueError("模型候选与 Recipe 槽位不匹配。")
            if str(asset["state"]) not in {"candidate", "verified", "incompatible"}:
                raise ValueError("该模型资产当前不能绑定。")
            if not role_accepts_extension(role, Path(str(asset["path"])).suffix):
                raise ValueError("模型容器类型与槽位不匹配。")
            connection.execute(
                "INSERT INTO model_bindings(role,asset_id,updated_at) VALUES(?,?,?) ON CONFLICT(role) DO UPDATE SET asset_id=excluded.asset_id,updated_at=excluded.updated_at",
                (role, asset_id, utc_now()),
            )
        return self.snapshot()

    def unbind(self, role: str) -> dict[str, Any]:
        if role not in ASSET_BY_ROLE:
            raise KeyError(role)
        with self.repository.transaction() as connection:
            connection.execute("DELETE FROM model_bindings WHERE role=?", (role,))
        return self.snapshot()

    def _bindings(self) -> dict[str, dict[str, Any]]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT b.role,a.* FROM model_bindings b JOIN model_assets a ON a.id=b.asset_id"
            ).fetchall()
        return {str(item["role"]): _row(item) for item in rows}

    def draft_digest(self) -> str | None:
        bindings = self._bindings()
        profile_digests = self._profile_configuration_digests(bindings)
        if not profile_digests:
            return None
        return canonical_sha256({
            "schemaVersion": 2,
            "recipeId": MODEL_RECIPE_ID,
            "recipeDigest": RECIPE_DIGEST,
            "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
            "profileConfigurationDigests": profile_digests,
        })

    def _profile_configuration_digests(
        self,
        bindings: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for profile, roles in PROFILE_ROLES.items():
            if all(
                role in bindings
                and bindings[role].get("state") == "verified"
                and bindings[role].get("verification_kind") in {"official", "structural"}
                for role in roles
            ):
                result[profile] = profile_configuration_digest(profile, bindings)
        return result

    def verify_draft(self) -> dict[str, Any]:
        def execute(operation_id: str, cancel: threading.Event) -> None:
            bindings = self._bindings()
            if not bindings:
                raise ValueError("至少绑定一个 Profile 所需的模型后才能验证。")
            summaries: dict[str, dict[str, Any]] = {}
            for index, role in enumerate(sorted(bindings)):
                if cancel.is_set():
                    return
                asset = bindings[role]
                recipe = ASSET_BY_ROLE[role]
                path = Path(str(asset["path"]))
                verification_kind: str | None = None
                contract_digest: str | None = None
                receipt_digest: str | None = None
                digest: str | None = None
                try:
                    root = self.root(str(asset["root_id"]))
                    safe = _safe_candidate(_safe_root(root["path"]), path)
                    digest = _sha256_candidate(safe, cancel)
                    if digest is None:
                        return
                    actual_bytes = safe.stat().st_size
                    if actual_bytes == recipe.bytes and digest == recipe.sha256:
                        verification_kind = "official"
                        contract_digest = RECIPE_DIGEST
                    else:
                        if self._asset_inspector is None:
                            raise RuntimeError("安全结构验证器尚未配置。")
                        receipt = self._asset_inspector(role, safe, cancel)
                        if (
                            receipt.get("state") != "passed"
                            or receipt.get("role") != role
                            or receipt.get("sha256") != digest
                            or int(receipt.get("bytes") or -1) != actual_bytes
                            or receipt.get("compatibilityPolicyDigest") != MODEL_COMPATIBILITY_POLICY_DIGEST
                        ):
                            raise RuntimeError("安全结构验证回执身份不匹配。")
                        receipt_digest = canonical_sha256(receipt)
                        verification_kind = "structural"
                        contract_digest = MODEL_COMPATIBILITY_POLICY_DIGEST
                        with self.repository.transaction() as connection:
                            connection.execute(
                                "INSERT INTO model_asset_verification_receipts(role,asset_sha256,compatibility_policy_digest,state,observation_digest,receipt_json,receipt_digest,created_at) "
                                "VALUES(?,?,?,'passed',?,?,?,?) ON CONFLICT(role,asset_sha256,compatibility_policy_digest) DO UPDATE SET state='passed',observation_digest=excluded.observation_digest,receipt_json=excluded.receipt_json,receipt_digest=excluded.receipt_digest,created_at=excluded.created_at",
                                (
                                    role,
                                    digest,
                                    MODEL_COMPATIBILITY_POLICY_DIGEST,
                                    receipt.get("observationDigest"),
                                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                                    receipt_digest,
                                    utc_now(),
                                ),
                            )
                    state, error = "verified", None
                except Exception as exc:
                    state, error = "incompatible", str(exc)
                    if digest:
                        failed_receipt = {
                            "state": "failed",
                            "role": role,
                            "sha256": digest,
                            "bytes": path.stat().st_size if path.is_file() else int(asset["bytes"]),
                            "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
                            "error": error,
                        }
                        failed_digest = canonical_sha256(failed_receipt)
                        with self.repository.transaction() as connection:
                            connection.execute(
                                "INSERT INTO model_asset_verification_receipts(role,asset_sha256,compatibility_policy_digest,state,observation_digest,receipt_json,receipt_digest,created_at) "
                                "VALUES(?,?,?,'failed',NULL,?,?,?) ON CONFLICT(role,asset_sha256,compatibility_policy_digest) DO UPDATE SET state='failed',observation_digest=NULL,receipt_json=excluded.receipt_json,receipt_digest=excluded.receipt_digest,created_at=excluded.created_at",
                                (role, digest, MODEL_COMPATIBILITY_POLICY_DIGEST, json.dumps(failed_receipt, ensure_ascii=False, sort_keys=True), failed_digest, utc_now()),
                            )
                with self.repository.transaction() as connection:
                    connection.execute(
                        "UPDATE model_assets SET bytes=?,sha256=?,state=?,verification_kind=?,verification_contract_digest=?,verification_receipt_digest=?,error_text=?,verified_at=?,updated_at=? WHERE id=?",
                        (
                            path.stat().st_size if path.is_file() else int(asset["bytes"]),
                            digest or str(asset["sha256"]),
                            state,
                            verification_kind,
                            contract_digest,
                            receipt_digest,
                            error,
                            utc_now() if state == "verified" else None,
                            utc_now(),
                            asset["id"],
                        ),
                    )
                summaries[role] = {
                    "state": state,
                    "verificationKind": verification_kind,
                    "error": error,
                }
                self._update_operation(operation_id, stage="verifying", progress=(index + 1) / len(bindings), detail={"roles": summaries})
            eligible = self._profile_configuration_digests(self._bindings())
            if not eligible:
                raise ValueError("没有任何 Profile 的必需角色全部通过验证；各槽位结果已保留。")
            self._update_operation(operation_id, detail={"roles": summaries, "eligibleProfiles": sorted(eligible)})

        return self._start("verify", execute)

    def self_test(self) -> dict[str, Any]:
        def execute(operation_id: str, cancel: threading.Event) -> None:
            if self._profile_tester is None:
                raise RuntimeError("Profile 自检器尚未配置。")
            bindings = self._bindings()
            digest = self.draft_digest()
            profile_digests = self._profile_configuration_digests(bindings)
            if digest is None or not profile_digests:
                raise ValueError("至少一个 Profile 的必需资产必须先通过验证。")
            payload = self.configuration_payload(digest, bindings)
            if self._self_test_begin is not None:
                self._self_test_begin(cancel)
            try:
                summaries: dict[str, dict[str, Any]] = {}
                for index, profile in enumerate(("high", "ultra")):
                    if cancel.is_set():
                        return
                    self._update_operation(operation_id, stage=f"self_test_{profile}", progress=index / 2, detail={"profile": profile})
                    runtime = self.runtime_descriptor(profile)
                    profile_digest = profile_digests.get(profile)
                    if profile_digest is None:
                        summaries[profile] = {
                            "state": "blocked",
                            "error": "该档位的必需角色尚未全部通过验证。",
                        }
                        self._update_operation(operation_id, progress=(index + 1) / 2, detail={"profiles": summaries})
                        continue
                    local_roles = [
                        role
                        for role in PROFILE_ROLES[profile]
                        if bindings[role].get("verification_kind") == "structural"
                    ]
                    qualification = "local-compatible" if local_roles else "official"
                    try:
                        receipt = self._profile_tester(profile, payload, cancel)
                        if receipt.get("state") != "passed":
                            raise RuntimeError(str(receipt.get("error") or f"{profile} GPU 自检失败。"))
                        if (
                            receipt.get("profile") != profile
                            or receipt.get("configurationDigest") != profile_digest
                            or receipt.get("runtimeDigest") != runtime["digest"]
                        ):
                            raise RuntimeError(f"{profile.upper()} GPU 自检回执身份不匹配。")
                        modes = [
                            item for item in receipt.get("executionModes") or []
                            if isinstance(item, dict)
                        ]
                        mode_names = {str(item.get("mode")) for item in modes if item.get("state") == "passed"}
                        if mode_names != {"full", "balanced", "constrained", "minimal"}:
                            raise RuntimeError(f"{profile.upper()} 四种执行模式未全部通过。")
                        gpu_identity = str(receipt.get("gpuIdentity") or receipt.get("device") or "unknown")
                        driver = str(receipt.get("driverVersion") or "unknown")
                        if gpu_identity == "unknown" or driver == "unknown":
                            raise RuntimeError(f"{profile.upper()} GPU 自检回执缺少 GPU/驱动身份。")
                        receipt = {
                            **receipt,
                            "profileConfigurationDigest": profile_digest,
                            "qualification": qualification,
                            "localCompatibleRoles": local_roles,
                            "assetIdentities": {
                                role: {
                                    "sha256": bindings[role]["sha256"],
                                    "verificationKind": bindings[role]["verification_kind"],
                                    "verificationReceiptDigest": bindings[role].get("verification_receipt_digest"),
                                }
                                for role in PROFILE_ROLES[profile]
                            },
                        }
                        state = "passed"
                    except Exception as exc:
                        receipt = {
                            "state": "failed",
                            "profile": profile,
                            "configurationDigest": profile_digest,
                            "profileConfigurationDigest": profile_digest,
                            "runtimeDigest": runtime["digest"],
                            "qualification": qualification,
                            "localCompatibleRoles": local_roles,
                            "error": str(exc),
                        }
                        gpu_identity = "unavailable"
                        driver = "unavailable"
                        state = "failed"
                    receipt_digest = canonical_sha256(receipt)
                    with self.repository.transaction() as connection:
                        connection.execute(
                            "INSERT INTO model_self_test_receipts(configuration_digest,profile,runtime_digest,gpu_identity,driver_version,state,receipt_json,receipt_digest,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(configuration_digest,profile) DO UPDATE SET runtime_digest=excluded.runtime_digest,gpu_identity=excluded.gpu_identity,driver_version=excluded.driver_version,state=excluded.state,receipt_json=excluded.receipt_json,receipt_digest=excluded.receipt_digest,created_at=excluded.created_at",
                            (profile_digest, profile, runtime["digest"], gpu_identity, driver, state, json.dumps(receipt, ensure_ascii=False, sort_keys=True), receipt_digest, utc_now()),
                        )
                    summaries[profile] = {
                        "state": state,
                        "profileConfigurationDigest": profile_digest,
                        "qualification": qualification,
                        "localCompatibleRoles": local_roles,
                        "receiptDigest": receipt_digest,
                        "error": receipt.get("error"),
                    }
                    self._update_operation(operation_id, progress=(index + 1) / 2, detail={"profiles": summaries})
                if not any(item.get("state") == "passed" for item in summaries.values()):
                    raise ValueError("没有任何 Profile 完成四模式自检；各档位结果已保留。")
            finally:
                if self._self_test_end is not None:
                    self._self_test_end()

        return self._start("self_test", execute)

    def activate(self) -> dict[str, Any]:
        def execute(operation_id: str, cancel: threading.Event) -> None:
            draft_digest = self.draft_digest()
            if draft_digest is None:
                raise ValueError("草稿配置不完整。")
            bindings = self._bindings()
            profile_states = self._profile_states(draft_digest, bindings)
            ready_profiles = [
                item for item in ("high", "ultra")
                if profile_states[item]["state"] == "ready"
            ]
            if not ready_profiles:
                raise ValueError("High 与 Ultra 均不可运行，验证结果会保留但不能激活。")
            receipt_rows = []
            with self.repository.connect() as connection:
                for profile in ready_profiles:
                    row = connection.execute(
                        "SELECT receipt_json FROM model_self_test_receipts WHERE configuration_digest=? AND profile=? AND state='passed'",
                        (profile_states[profile]["profileConfigurationDigest"], profile),
                    ).fetchone()
                    if row is not None:
                        receipt_rows.append(row)
            bound_gpu_uuids = {
                str(json.loads(str(item["receipt_json"] or "{}")).get("gpuUuid") or "")
                for item in receipt_rows
            }
            bound_gpu_uuids.discard("")
            if len(bound_gpu_uuids) > 1:
                raise ValueError("同一活动配置的 High/Ultra 自检回执不能绑定不同 GPU。")
            active_roles = {
                role for profile in ready_profiles for role in PROFILE_ROLES[profile]
            }
            active_bindings = {
                role: value for role, value in bindings.items() if role in active_roles
            }
            digest = canonical_sha256(
                {
                    "schemaVersion": 2,
                    "recipeId": MODEL_RECIPE_ID,
                    "recipeDigest": RECIPE_DIGEST,
                    "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST,
                    "profiles": {
                        profile: profile_states[profile]["profileConfigurationDigest"]
                        for profile in ready_profiles
                    },
                }
            )
            payload = self.configuration_payload(digest, active_bindings)
            self._update_operation(operation_id, stage="draining", progress=0.1, detail={"configurationDigest": digest, "profiles": ready_profiles})
            if self._activator is not None:
                detail = self._activator(payload)
                self._update_operation(operation_id, stage="switching", progress=0.8, detail=detail)
            now = utc_now()
            with self.repository.transaction() as connection:
                connection.execute("UPDATE model_configurations SET active=0 WHERE active=1")
                connection.execute(
                    "INSERT INTO model_configurations(digest,recipe_id,recipe_digest,state,active,profile_states_json,schema_version,created_at,activated_at) "
                    "VALUES(?,?,?,'ready',1,?,2,?,?) ON CONFLICT(digest) DO UPDATE SET state='ready',active=1,profile_states_json=excluded.profile_states_json,schema_version=2,activated_at=excluded.activated_at",
                    (digest, MODEL_RECIPE_ID, RECIPE_DIGEST, json.dumps({profile: profile_states[profile] for profile in ready_profiles}, ensure_ascii=False, sort_keys=True), now, now),
                )
                connection.execute("DELETE FROM model_configuration_assets WHERE configuration_digest=?", (digest,))
                connection.executemany(
                    "INSERT INTO model_configuration_assets(configuration_digest,role,asset_id) VALUES(?,?,?)",
                    [(digest, role, value["id"]) for role, value in active_bindings.items()],
                )
                connection.execute(
                    "DELETE FROM model_configurations WHERE digest<>?", (digest,)
                )
                connection.execute(
                    "DELETE FROM model_assets WHERE id NOT IN (SELECT asset_id FROM model_bindings) "
                    "AND id NOT IN (SELECT asset_id FROM model_configuration_assets)"
                )
            self.repository.log(None, "info", "model.configuration_activated", {"configurationDigest": digest, "profiles": ready_profiles, "qualifications": {profile: profile_states[profile]["qualification"] for profile in ready_profiles}}, component="model")

        return self._start("activate", execute)

    def configuration_payload(self, digest: str, bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        values = bindings
        schema_version = 2 if bindings is not None else 1
        if values is None:
            with self.repository.connect() as connection:
                configuration = connection.execute(
                    "SELECT schema_version FROM model_configurations WHERE digest=?",
                    (digest,),
                ).fetchone()
                if configuration is not None:
                    schema_version = int(configuration["schema_version"] or 1)
                rows = connection.execute(
                    "SELECT ca.role,a.* FROM model_configuration_assets ca JOIN model_assets a ON a.id=ca.asset_id WHERE ca.configuration_digest=?",
                    (digest,),
                ).fetchall()
            values = {str(item["role"]): _row(item) for item in rows}
        profile_digests = self._profile_configuration_digests(values)
        receipt_rows: list[Any] = []
        with self.repository.connect() as connection:
            for profile, profile_digest in profile_digests.items():
                receipt = connection.execute(
                    "SELECT profile,receipt_digest,receipt_json FROM model_self_test_receipts WHERE configuration_digest=? AND profile=? AND state='passed'",
                    (profile_digest, profile),
                ).fetchone()
                if receipt is None and schema_version == 1:
                    receipt = connection.execute(
                        "SELECT profile,receipt_digest,receipt_json FROM model_self_test_receipts WHERE configuration_digest=? AND profile=? AND state='passed'",
                        (digest, profile),
                    ).fetchone()
                if receipt is not None:
                    receipt_rows.append(receipt)
            structural_receipts = {
                role: connection.execute(
                    "SELECT receipt_json FROM model_asset_verification_receipts WHERE role=? AND asset_sha256=? AND compatibility_policy_digest=? AND state='passed'",
                    (role, value["sha256"], MODEL_COMPATIBILITY_POLICY_DIGEST),
                ).fetchone()
                for role, value in values.items()
                if value.get("verification_kind") == "structural"
            }
        payload = {
            "schemaVersion": schema_version,
            "configurationDigest": digest,
            "recipeId": MODEL_RECIPE_ID,
            "recipeDigest": RECIPE_DIGEST,
            "compatibilityPolicyDigest": MODEL_COMPATIBILITY_POLICY_DIGEST if schema_version == 2 else None,
            "profileConfigurationDigests": profile_digests if schema_version == 2 else {},
            "assets": {
                role: {
                    "assetId": value["id"],
                    "modelId": ASSET_BY_ROLE[role].model_id,
                    "role": role,
                    "bytes": int(value["bytes"]),
                    "sha256": value["sha256"],
                    "revision": ASSET_BY_ROLE[role].revision,
                    "path": value["path"],
                    **(
                        {
                            "verificationKind": value.get("verification_kind"),
                            "verificationContractDigest": value.get("verification_contract_digest"),
                            "verificationReceiptDigest": value.get("verification_receipt_digest"),
                            "verificationReceipt": (
                                json.loads(str(structural_receipts[role]["receipt_json"]))
                                if role in structural_receipts and structural_receipts[role] is not None
                                else None
                            ),
                        }
                        if schema_version == 2
                        else {}
                    ),
                }
                for role, value in sorted(values.items())
            },
            "selfTestReceipts": {
                str(item["profile"]): str(item["receipt_digest"]) for item in receipt_rows
            },
            "profileExecutionReceipts": {
                str(item["profile"]): json.loads(str(item["receipt_json"] or "{}"))
                for item in receipt_rows
            },
        }
        if schema_version == 1:
            payload.pop("compatibilityPolicyDigest")
            payload.pop("profileConfigurationDigests")
        return payload

    def active_configuration(self) -> dict[str, Any] | None:
        with self.repository.connect() as connection:
            row = connection.execute("SELECT * FROM model_configurations WHERE active=1 LIMIT 1").fetchone()
        return self.configuration_payload(str(row["digest"])) if row is not None else None

    @staticmethod
    def _public_configuration(payload: dict[str, Any]) -> dict[str, Any]:
        result = {**payload, "assets": {}}
        for role, value in (payload.get("assets") or {}).items():
            public_asset = dict(value)
            public_asset.pop("verificationReceipt", None)
            result["assets"][role] = public_asset
        return result

    def profile_states_for_configuration(self, digest: str) -> dict[str, dict[str, Any]]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT ca.role,a.* FROM model_configuration_assets ca JOIN model_assets a ON a.id=ca.asset_id WHERE ca.configuration_digest=?",
                (digest,),
            ).fetchall()
        return self._profile_states(digest, {str(item["role"]): _row(item) for item in rows})

    def _profile_states(
        self,
        digest: str | None,
        bindings: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        bindings = bindings if bindings is not None else self._bindings()
        profile_digests = self._profile_configuration_digests(bindings)
        result: dict[str, dict[str, Any]] = {}
        for profile, roles in PROFILE_ROLES.items():
            blockers: list[str] = []
            execution_modes: list[dict[str, Any]] = []
            local_roles: list[str] = []
            for role in roles:
                asset = bindings.get(role)
                if asset is None:
                    blockers.append(f"未绑定 {ASSET_BY_ROLE[role].display_name}")
                elif asset.get("state") != "verified":
                    blockers.append(f"{ASSET_BY_ROLE[role].display_name} 尚未通过身份或结构验证")
                else:
                    verification_kind = str(asset.get("verification_kind") or "")
                    if verification_kind not in {"official", "structural"}:
                        blockers.append(f"{ASSET_BY_ROLE[role].display_name} 缺少验证类别")
                    if verification_kind == "structural":
                        local_roles.append(role)
                        if (
                            asset.get("verification_contract_digest") != MODEL_COMPATIBILITY_POLICY_DIGEST
                            or not asset.get("verification_receipt_digest")
                        ):
                            blockers.append(f"{ASSET_BY_ROLE[role].display_name} 结构回执已失效")
                        else:
                            with self.repository.connect() as connection:
                                structural_receipt = connection.execute(
                                    "SELECT receipt_json,receipt_digest,state FROM model_asset_verification_receipts WHERE role=? AND asset_sha256=? AND compatibility_policy_digest=?",
                                    (role, asset.get("sha256"), MODEL_COMPATIBILITY_POLICY_DIGEST),
                                ).fetchone()
                            try:
                                structural_payload = json.loads(
                                    str(structural_receipt["receipt_json"] or "{}")
                                ) if structural_receipt is not None else {}
                            except json.JSONDecodeError:
                                structural_payload = {}
                            if (
                                structural_receipt is None
                                or str(structural_receipt["state"]) != "passed"
                                or str(structural_receipt["receipt_digest"]) != str(asset.get("verification_receipt_digest"))
                                or canonical_sha256(structural_payload) != str(asset.get("verification_receipt_digest"))
                            ):
                                blockers.append(f"{ASSET_BY_ROLE[role].display_name} 结构回执摘要已损坏")
                    path = Path(str(asset.get("path") or ""))
                    try:
                        if not path.is_file() or path.is_symlink() or path.stat().st_size != int(asset["bytes"]):
                            raise OSError("asset identity changed")
                    except OSError:
                        blockers.append(f"{ASSET_BY_ROLE[role].display_name} 文件已变化或消失")
            receipt = None
            profile_digest = profile_digests.get(profile)
            if profile_digest:
                with self.repository.connect() as connection:
                    receipt = connection.execute(
                        "SELECT * FROM model_self_test_receipts WHERE configuration_digest=? AND profile=?",
                        (profile_digest, profile),
                    ).fetchone()
                    if receipt is None and digest:
                        receipt = connection.execute(
                            "SELECT * FROM model_self_test_receipts WHERE configuration_digest=? AND profile=?",
                            (digest, profile),
                        ).fetchone()
            runtime = self.runtime_descriptor(profile)
            if not runtime["installed"]:
                blockers.append(f"{profile.upper()} 固定运行时或产品清单不可用")
            if receipt is None or str(receipt["state"]) != "passed":
                blockers.append(f"{profile.upper()} 尚未完成目标 GPU 自检")
            elif str(receipt["runtime_digest"]) != runtime["digest"]:
                blockers.append(f"{profile.upper()} 固定运行时身份已变化，必须重新自检")
            else:
                try:
                    receipt_payload = json.loads(str(receipt["receipt_json"] or "{}"))
                except json.JSONDecodeError:
                    receipt_payload = {}
                if canonical_sha256(receipt_payload) != str(receipt["receipt_digest"]):
                    blockers.append(f"{profile.upper()} 自检回执摘要已损坏")
                expected_profile_digest = str(
                    receipt_payload.get("profileConfigurationDigest")
                    or receipt_payload.get("configurationDigest")
                    or ""
                )
                if profile_digest and expected_profile_digest not in {profile_digest, str(digest or "")}:
                    blockers.append(f"{profile.upper()} Profile 配置摘要已变化，必须重新自检")
                execution_modes = [
                    dict(item)
                    for item in receipt_payload.get("executionModes") or []
                    if isinstance(item, dict)
                ]
                expected_gpu = str(receipt_payload.get("gpuUuid") or receipt_payload.get("gpuIdentity") or "")
                current = _current_gpu(expected_gpu or None)
                expected_driver = str(receipt_payload.get("driverVersion") or "")
                if not current or (
                    expected_gpu and current.get("uuid") != expected_gpu
                ) or (
                    expected_driver and current.get("driverVersion") != expected_driver
                ):
                    blockers.append(f"{profile.upper()} 目标 GPU 或驱动已变化，必须重新自检")
            state = "ready" if not blockers else "blocked"
            result[profile] = {
                "profile": profile,
                "state": state,
                "blockers": blockers,
                "runtime": runtime,
                "profileConfigurationDigest": profile_digest,
                "qualification": "local-compatible" if local_roles else "official",
                "localCompatibleRoles": local_roles,
                "receiptDigest": str(receipt["receipt_digest"]) if receipt is not None else None,
                "executionModes": execution_modes,
            }
        return result

    def snapshot(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            active = connection.execute("SELECT * FROM model_configurations WHERE active=1 LIMIT 1").fetchone()
        bindings = self._bindings()
        digest = self.draft_digest()
        slots = []
        for recipe in ASSETS:
            selected = bindings.get(recipe.role)
            slots.append({
                **recipe.as_dict(),
                "state": str(selected.get("state")) if selected else "unbound",
                "binding": self._asset_dto(selected) if selected else None,
                "error": selected.get("error_text") if selected else None,
            })
        return {
            "recipe": RECIPE,
            "compatibilityPolicy": {
                **MODEL_COMPATIBILITY_POLICY,
                "digest": MODEL_COMPATIBILITY_POLICY_DIGEST,
            },
            "slots": slots,
            "profiles": self._profile_states(digest),
            "draftConfigurationDigest": digest,
            "activeConfiguration": self._public_configuration(self.configuration_payload(str(active["digest"]))) if active is not None else None,
            "operations": self.operations(),
        }

    @staticmethod
    def _asset_dto(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "rootId": value["root_id"],
            "role": value["role"],
            "modelId": value["model_id"],
            "path": value["path"],
            "bytes": int(value["bytes"]),
            "sha256": value["sha256"],
            "state": value["state"],
            "verificationKind": value.get("verification_kind"),
            "verificationContractDigest": value.get("verification_contract_digest"),
            "verificationReceiptDigest": value.get("verification_receipt_digest"),
            "error": value.get("error_text"),
            "verifiedAt": value.get("verified_at"),
        }
