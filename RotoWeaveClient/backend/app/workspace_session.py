from __future__ import annotations

import hashlib
import json
import os
import threading
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts.windows_file_dialog import choose_windows_folder

from .config import Settings
from .runtime_repository import RuntimeRepository
from .workspace_format import (
    WORKSPACE_MANIFEST,
    WorkspaceChangedError,
    WorkspaceError,
    WorkspaceFormatError,
    atomic_write_json,
    create_workspace,
    read_json,
    resolve_workspace_path,
    validate_workspace_manifest,
    verify_workspace_atomic_replace,
    workspace_writable_reason,
)
from .workspace_repository import WorkspaceRepository, recover_interrupted_delivery


REQUEST_WORKSPACE_EPOCH: ContextVar[int | None] = ContextVar(
    "rotoweave_request_workspace_epoch", default=None
)
REQUEST_REVISION_ID: ContextVar[str | None] = ContextVar(
    "rotoweave_request_revision_id", default=None
)
REQUEST_API_PATH: ContextVar[str | None] = ContextVar(
    "rotoweave_request_api_path", default=None
)


class WorkspaceClosedError(WorkspaceError):
    """A business operation was requested without an open workspace."""


@dataclass(slots=True)
class WorkspaceSnapshot:
    state: str
    epoch: int
    workspace_id: str | None
    name: str | None
    root: Path | None
    read_only: bool
    validation: str
    conflict: str | None


class WorkspaceSessionManager:
    """Own exactly one workspace repository and its disposable runtime state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._guard = threading.RLock()
        self._transition = threading.RLock()
        self._write_coordinator = threading.RLock()
        self._state = "Closed"
        self._epoch = 0
        self._repository: WorkspaceRepository | None = None
        self._runtime: RuntimeRepository | None = None
        self._root: Path | None = None
        self._workspace_id: str | None = None
        self._workspace_name: str | None = None
        self._read_only = False
        self._validation = "not_checked"
        self._conflict: str | None = None
        self._lock_handle: Any = None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def runtime_root(self) -> Path:
        if self._root is None:
            return self.settings.local_state_root / "temp" / "closed"
        token = self._path_token(self._root)
        return self.settings.local_state_root / "workspaces" / token

    @staticmethod
    def _path_token(root: Path) -> str:
        normalized = root.resolve(strict=False).as_posix().casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def require_repository(self) -> WorkspaceRepository:
        repository = self._repository
        if repository is None or self._state != "Open":
            raise WorkspaceClosedError("请先新建或打开一个 RotoWeave 工作区。")
        if self._conflict:
            raise WorkspaceChangedError(self._conflict)
        return repository

    def snapshot(self, *, expose_path: bool) -> dict[str, Any]:
        value = {
            "state": self._state,
            "epoch": self._epoch,
            "workspaceId": self._workspace_id,
            "name": self._workspace_name,
            "readOnly": self._read_only,
            "validation": self._validation,
            "conflict": self._conflict,
            "canMutate": self._state == "Open" and not self._read_only and not self._conflict,
            "canManageWorkspace": bool(expose_path),
        }
        if expose_path:
            value["root"] = str(self._root) if self._root else None
            value["recent"] = self.recent_workspaces()
        return value

    def recent_workspaces(self) -> list[dict[str, Any]]:
        path = self.settings.recent_workspaces_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        entries = payload.get("workspaces") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        result: list[dict[str, Any]] = []
        for item in entries[:10]:
            if not isinstance(item, dict) or not str(item.get("root") or ""):
                continue
            result.append(
                {
                    "root": str(item["root"]),
                    "name": str(item.get("name") or Path(str(item["root"])).name),
                    "workspaceId": item.get("workspaceId"),
                    "available": (Path(str(item["root"])) / WORKSPACE_MANIFEST).is_file(),
                }
            )
        return result

    def _remember(self, root: Path, name: str, workspace_id: str) -> None:
        entries = [
            item
            for item in self.recent_workspaces()
            if Path(str(item["root"])).resolve(strict=False) != root
        ]
        entries.insert(0, {"root": str(root), "name": name, "workspaceId": workspace_id})
        self.settings.local_state_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.settings.recent_workspaces_path,
            {"schemaVersion": 1, "workspaces": entries[:10]},
        )

    def _acquire_lock(self, runtime_root: Path) -> Any:
        runtime_root.mkdir(parents=True, exist_ok=True)
        handle = (runtime_root / "writer.lock").open("a+b")
        handle.seek(0)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                handle.close()
                raise WorkspaceChangedError("该工作区已被另一个 RotoWeave 实例写入。") from exc
        return handle

    @staticmethod
    def _release_lock(handle: Any) -> None:
        if handle is None:
            return
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()

    def create(self, root: Path, name: str) -> dict[str, Any]:
        with self._transition:
            return self._create(root, name)

    def _create(self, root: Path, name: str) -> dict[str, Any]:
        requested = root.expanduser().absolute()
        reason = workspace_writable_reason(requested)
        if reason:
            raise WorkspaceFormatError(reason)
        target = requested.resolve(strict=False)
        create_workspace(target, name)
        try:
            return self._open(target)
        except Exception:
            # The new user folder remains inspectable; never delete it after a
            # failed open because external processes may already have touched it.
            raise

    def open(
        self,
        root: Path,
        *,
        exclusive: bool = True,
    ) -> dict[str, Any]:
        with self._transition:
            return self._open(
                root,
                exclusive=exclusive,
            )

    def _open(
        self,
        root: Path,
        *,
        exclusive: bool = True,
    ) -> dict[str, Any]:
        requested = root.expanduser().absolute()
        reason = workspace_writable_reason(requested)
        if reason:
            raise WorkspaceFormatError(reason)
        target = requested.resolve(strict=False)
        if self._root == target and self._state == "Open":
            return self.snapshot(expose_path=True)
        manifest, _ = read_json(
            resolve_workspace_path(target, WORKSPACE_MANIFEST)
        )
        validate_workspace_manifest(manifest)
        workspace_id = str(manifest["workspaceId"])
        runtime_root = (
            self.settings.local_state_root
            / "workspaces"
            / self._path_token(target)
        )
        try:
            candidate_lock = self._acquire_lock(runtime_root) if exclusive else None
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceFormatError("无法建立工作区本机写入锁。") from exc
        try:
            verify_workspace_atomic_replace(target)
            runtime = RuntimeRepository(runtime_root / "runtime.sqlite3", workspace_id)
            runtime.initialize()
            recover_interrupted_delivery(target, runtime_root, workspace_id)
            repository = WorkspaceRepository(target, runtime, writable=True)
        except Exception as exc:
            self._release_lock(candidate_lock)
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceFormatError(
                "工作区运行态或业务文件无法完整打开。"
            ) from exc
        with self._write_coordinator:
            with self._guard:
                if self._repository is not None and self._repository.has_active_work():
                    self._release_lock(candidate_lock)
                    raise WorkspaceChangedError("当前工作区仍有排队或运行中的任务，暂时不能切换。")
                old_lock = self._lock_handle
                self._state = "Opening"
                self._repository = repository
                self._runtime = runtime
                self._root = target
                self._workspace_id = workspace_id
                self._workspace_name = str(manifest["name"])
                self._read_only = False
                self._validation = "valid"
                self._conflict = None
                self._lock_handle = candidate_lock
                self._epoch += 1
                self._state = "Open"
                self._release_lock(old_lock)
                self._remember(target, self._workspace_name, workspace_id)
        return self.snapshot(expose_path=True)

    def open_recent(self) -> bool:
        for item in self.recent_workspaces():
            if not item.get("available"):
                continue
            try:
                self.open(Path(str(item["root"])))
                return True
            except Exception:
                continue
        return False

    def validate(self, *, full_hash: bool) -> dict[str, Any]:
        repository = self.require_repository()
        try:
            result = repository.validate(full_hash=full_hash)
        except (WorkspaceChangedError, WorkspaceFormatError) as exc:
            self.mark_conflict(str(exc))
            raise
        self._validation = "valid"
        return result

    def reload(self) -> dict[str, Any]:
        """Atomically rebuild the current session from its on-disk workspace."""

        with self._transition:
            with self._write_coordinator:
                with self._guard:
                    if self._repository is None or self._root is None:
                        raise WorkspaceError("当前没有打开的工作区。")
                    if self._repository.has_active_work():
                        raise WorkspaceChangedError(
                            "当前工作区仍有排队或运行中的任务，暂时不能刷新。"
                        )
                    target = self._root
                reason = workspace_writable_reason(target)
                if reason:
                    raise WorkspaceFormatError(reason)
                manifest, _ = read_json(
                    resolve_workspace_path(target, WORKSPACE_MANIFEST)
                )
                validate_workspace_manifest(manifest)
                verify_workspace_atomic_replace(target)
                workspace_id = str(manifest["workspaceId"])
                runtime_root = (
                    self.settings.local_state_root
                    / "workspaces"
                    / self._path_token(target)
                )
                try:
                    runtime = RuntimeRepository(
                        runtime_root / "runtime.sqlite3", workspace_id
                    )
                    runtime.initialize()
                    recover_interrupted_delivery(
                        target, runtime_root, workspace_id
                    )
                    repository = WorkspaceRepository(
                        target, runtime, writable=True
                    )
                except Exception as exc:
                    if isinstance(exc, WorkspaceError):
                        raise
                    raise WorkspaceFormatError(
                        "工作区运行态或业务文件无法完整重新加载。"
                    ) from exc
                with self._guard:
                    self._state = "Opening"
                    self._repository = repository
                    self._runtime = runtime
                    self._workspace_id = workspace_id
                    self._workspace_name = str(manifest["name"])
                    self._read_only = False
                    self._validation = "valid"
                    self._conflict = None
                    self._epoch += 1
                    self._state = "Open"
                    self._remember(
                        target, self._workspace_name, workspace_id
                    )
        return self.snapshot(expose_path=True)

    def has_active_work(self) -> bool:
        repository = self._repository
        return bool(
            repository is not None
            and self._state == "Open"
            and repository.has_active_work()
        )

    def mark_conflict(self, message: str) -> None:
        with self._guard:
            self._conflict = message
            self._read_only = True
            self._validation = "changed"

    def close(self, *, prepare: bool) -> dict[str, Any]:
        with self._transition:
            return self._close(prepare=prepare)

    def _close(self, *, prepare: bool) -> dict[str, Any]:
        with self._write_coordinator:
            with self._guard:
                if self._repository is None:
                    return self.snapshot(expose_path=True)
                if self._repository.has_active_work():
                    raise WorkspaceChangedError("仍有排队或运行中的任务，暂时不能关闭工作区。")
                repository = self._repository
            try:
                validation = repository.prepare_for_close() if prepare else None
            except (WorkspaceChangedError, WorkspaceFormatError) as exc:
                self.mark_conflict(str(exc))
                raise
            with self._guard:
                self._state = "Closing"
                self._repository = None
                self._runtime = None
                self._root = None
                self._workspace_id = None
                self._workspace_name = None
                self._read_only = False
                self._validation = "not_checked"
                self._conflict = None
                self._epoch += 1
                self._state = "Closed"
                lock_handle = self._lock_handle
                self._lock_handle = None
                self._release_lock(lock_handle)
        return {**self.snapshot(expose_path=True), "prepared": validation}

    def shutdown(self) -> None:
        """Release process-local resources; queued jobs remain recoverable."""

        with self._guard:
            self._repository = None
            self._runtime = None
            self._root = None
            self._workspace_id = None
            self._workspace_name = None
            self._state = "Closed"
            self._epoch += 1
            handle = self._lock_handle
            self._lock_handle = None
            self._release_lock(handle)


_READ_ONLY_REPOSITORY_METHODS = frozenset(
    {
        "assert_http_revision",
        "current_http_revision",
        "current_target_revision",
        "get_job",
        "get_material_source",
        "get_material_variant",
        "get_size_profile",
        "has_active_work",
        "http_revision_target",
        "list_jobs",
        "list_size_profiles",
        "find_material_source_by_content_hash",
        "workspace_domain",
    }
)


class WorkspaceRepositoryGateway:
    """Serialize current-repository access while a workspace session can switch."""

    def __init__(self, session: WorkspaceSessionManager):
        self.session = session

    @property
    def path(self) -> Path:
        return self.session.require_repository().runtime.path

    @property
    def root(self) -> Path:
        repository = self.session.require_repository()
        return repository.root

    def initialize(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        repository = self.session.require_repository()
        attribute = getattr(repository, name)
        if not callable(attribute):
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            expected_epoch = REQUEST_WORKSPACE_EPOCH.get()
            if name in _READ_ONLY_REPOSITORY_METHODS:
                current = self.session.require_repository()
                if (
                    expected_epoch is not None
                    and expected_epoch != self.session.epoch
                ):
                    raise WorkspaceChangedError(
                        "工作区已在请求处理期间切换，旧操作未继续。"
                    )
                expected_revision_id = REQUEST_REVISION_ID.get()
                request_api_path = REQUEST_API_PATH.get()
                try:
                    if expected_revision_id and request_api_path:
                        current.assert_http_revision(
                            request_api_path, expected_revision_id
                        )
                    result = getattr(current, name)(*args, **kwargs)
                    if (
                        self.session.epoch != expected_epoch
                        and expected_epoch is not None
                    ) or self.session.require_repository() is not current:
                        raise WorkspaceChangedError(
                            "工作区已在读取期间切换，请重新加载。"
                        )
                    return result
                except WorkspaceChangedError as exc:
                    self.session.mark_conflict(str(exc))
                    raise
            with self.session._write_coordinator:
                if (
                    expected_epoch is not None
                    and expected_epoch != self.session.epoch
                ):
                    raise WorkspaceChangedError(
                        "工作区已在请求处理期间切换，旧操作未写入。"
                    )
                current = self.session.require_repository()
                expected_revision_id = REQUEST_REVISION_ID.get()
                request_api_path = REQUEST_API_PATH.get()
                if expected_revision_id and request_api_path:
                    current.assert_http_revision(
                        request_api_path, expected_revision_id
                    )
                current_attribute = getattr(current, name)
                try:
                    result = current_attribute(*args, **kwargs)
                    if expected_revision_id and request_api_path:
                        # A request can publish one aggregate and then enqueue
                        # work for it. Advance only to the revision produced by
                        # this successful call; the next call still rejects any
                        # intervening mutation from another request.
                        next_revision_id = current.current_http_revision(
                            request_api_path
                        )
                        if next_revision_id:
                            REQUEST_REVISION_ID.set(next_revision_id)
                    return result
                except WorkspaceChangedError as exc:
                    self.session.mark_conflict(str(exc))
                    raise

        return guarded


def choose_workspace_folder() -> str | None:
    """Open the host-only native folder chooser without exposing paths remotely."""

    try:
        selected = choose_windows_folder("选择 RotoWeave 工作区文件夹")
    except RuntimeError as exc:
        raise WorkspaceError(str(exc)) from exc
    return str(selected) if selected else None
