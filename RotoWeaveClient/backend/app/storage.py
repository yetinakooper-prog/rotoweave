from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .config import Settings


SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def safe_filename(name: str, fallback: str = "asset") -> str:
    cleaned = SAFE_NAME.sub("_", Path(name).name).strip("._")
    return cleaned[:160] or fallback


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class ObjectStore:
    def __init__(self, settings: Settings, session: Any | None = None):
        self.settings = settings
        self.session = session
        self.database: Any | None = None
        # Object creation and its following database commit are serialized by
        # API callers through object_import_transaction().  This prevents two
        # identical concurrent uploads from deleting each other's shared blob
        # during rollback.
        self._object_import_lock = threading.RLock()
        self._character_locks_guard = threading.Lock()
        self._character_locks: dict[str, threading.RLock] = {}

    def bind_database(self, database: Any) -> None:
        self.database = database

    @property
    def runtime_root(self) -> Path:
        if self.session is not None:
            return Path(self.session.runtime_root)
        if self.database is not None and hasattr(self.database, "session"):
            return Path(self.database.session.runtime_root)
        return self.settings.local_state_root

    @contextmanager
    def object_import_transaction(self) -> Iterator[None]:
        with self._object_import_lock:
            yield

    @contextmanager
    def character_transaction(self, character_id: str) -> Iterator[None]:
        """Serialize file-tree publication and cleanup for one character."""

        with self._character_locks_guard:
            lock = self._character_locks.setdefault(
                character_id, threading.RLock()
            )
        with lock:
            yield

    def object_path(self, digest: str, suffix: str = "") -> Path:
        normalized_suffix = suffix if suffix.startswith(".") or not suffix else f".{suffix}"
        return self.runtime_root / "incoming" / f"{digest}{normalized_suffix.lower()}"

    def resolve_incoming(self, raw_path: object) -> Path:
        candidate = Path(str(raw_path or "")).resolve(strict=True)
        incoming_root = (self.runtime_root / "incoming").resolve()
        if incoming_root not in candidate.parents or not candidate.is_file():
            raise ValueError("临时导入文件不属于当前工作区运行目录。")
        return candidate

    def put_stream(
        self, stream: BinaryIO, filename: str, max_bytes: int | None = None
    ) -> tuple[Path, str, int, bool]:
        with self._object_import_lock:
            temp_root = self.runtime_root / "temp"
            temp_root.mkdir(parents=True, exist_ok=True)
            temp_path = temp_root / (
                f"upload_{os.getpid()}_{uuid.uuid4().hex}_"
                f"{safe_filename(filename)}.part"
            )
            digest = hashlib.sha256()
            total = 0
            try:
                with temp_path.open("xb") as destination:
                    while chunk := stream.read(1024 * 1024):
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            raise ValueError(f"上传文件不能超过 {max_bytes} 字节。")
                        digest.update(chunk)
                        destination.write(chunk)
                sha256 = digest.hexdigest()
                target = self.object_path(sha256, Path(filename).suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                created = not target.exists()
                if created:
                    temp_path.replace(target)
                else:
                    temp_path.unlink(missing_ok=True)
                return target, sha256, total, created
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

    def character_dir(self, character_id: str, stage: str | None = None) -> Path:
        if self.database is not None:
            path = self.database.character_storage_dir(character_id)
        else:
            path = self.settings.data_root / "characters" / character_id
        if stage in {"export-staging", "exports"}:
            path = self.runtime_root / "delivery" / character_id / str(stage)
        elif stage == "core-reference":
            path = path / "core" / "generations"
        elif stage == "atlas-builds":
            path = path / "atlas"
        elif stage:
            path /= stage
        if self.database is not None and stage not in {"export-staging", "exports"}:
            path = self.database.ensure_workspace_directory(path)
        else:
            path.mkdir(parents=True, exist_ok=True)
        return path
