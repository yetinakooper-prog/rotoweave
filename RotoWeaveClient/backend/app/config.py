from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from contracts.paths import resolve_models_root
from contracts.brand_migration import migrate_client_local_app_data

from contracts.product import (
    APPLICATION_DATA_DIRECTORY,
    RUNTIME_API_PORT,
    SESSION_COOKIE_NAME,
)

BOOTSTRAP_TOKEN_TTL_SECONDS = 300.0


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
    return Path(__file__).resolve().parents[2]


def _default_data_root() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = compatible_environment_value("LOCALAPPDATA")
    if local_app_data:
        return migrate_client_local_app_data(Path(local_app_data))
    return Path.home() / f".{APPLICATION_DATA_DIRECTORY.lower()}"


@dataclass(slots=True)
class Settings:
    app_name: str = "RotoWeave"
    host: str = "127.0.0.1"
    port: int = field(
        default_factory=lambda: int(compatible_environment_value("ROTOWEAVE_PORT", str(RUNTIME_API_PORT)))
    )
    cpu_workers: int = field(
        default_factory=lambda: max(
            1,
            min(
                8,
                int(
                    compatible_environment_value(
                        "ROTOWEAVE_CPU_WORKERS",
                        str(min(8, max(2, os.cpu_count() or 4))),
                    )
                ),
            ),
        )
    )
    data_root: Path = field(default_factory=_default_data_root)
    local_state_root: Path = field(init=False)
    runtime_root: Path = field(default_factory=_runtime_root)
    session_token: str = field(
        default_factory=lambda: compatible_environment_value("ROTOWEAVE_SESSION_TOKEN") or secrets.token_urlsafe(24)
    )
    require_session_token: bool = field(
        default_factory=lambda: compatible_environment_value("ROTOWEAVE_REQUIRE_SESSION_TOKEN", "0") == "1"
        or bool(getattr(sys, "frozen", False))
    )
    _bootstrap_lock: threading.RLock = field(init=False, default_factory=threading.RLock, repr=False)
    _bootstrap_tokens: dict[str, float] = field(
        init=False, default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self.local_state_root = self.data_root

    @property
    def listen_host(self) -> str:
        configured = compatible_environment_value("ROTOWEAVE_HOST", "127.0.0.1").strip().lower()
        if configured not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "RotoWeave 4.0 本地服务只能绑定 loopback；远程处理请使用独立抠图服务。"
            )
        return "127.0.0.1"

    @property
    def bundled_birefnet_onnx(self) -> Path:
        return (
            resolve_models_root(self.runtime_root)
            / "application"
            / "basic"
            / "birefnet-lite-matting.onnx"
        ).resolve(strict=False)

    def create_bootstrap_token(self) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + BOOTSTRAP_TOKEN_TTL_SECONDS
        with self._bootstrap_lock:
            now = time.monotonic()
            self._bootstrap_tokens = {
                value: expiry
                for value, expiry in self._bootstrap_tokens.items()
                if expiry > now
            }
            self._bootstrap_tokens[token] = expires_at
        return token

    def consume_bootstrap_token(self, supplied: str) -> bool:
        if not supplied:
            return False
        with self._bootstrap_lock:
            expires_at = self._bootstrap_tokens.pop(supplied, None)
        return expires_at is not None and expires_at > time.monotonic()

    def ensure_directories(self) -> None:
        for path in (
            self.local_state_root,
            self.local_state_root / "workspaces",
            self.local_state_root / "temp",
            self.local_state_root / "logs",
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def remote_matting_url(self) -> str | None:
        value = (compatible_environment_value("ROTOWEAVE_REMOTE_MATTING_URL") or "").strip()
        return value or None

    @property
    def recent_workspaces_path(self) -> Path:
        return self.local_state_root / "recent-workspaces.json"

    @property
    def frontend_candidates(self) -> list[Path]:
        configured = compatible_environment_value("ROTOWEAVE_FRONTEND_ROOT")
        candidates = [
            Path(configured).expanduser() if configured else None,
            self.runtime_root / "frontend",
            self.runtime_root / "runtime" / "frontend",
            self.runtime_root / "dist",
            self.runtime_root / "release" / "frontend",
        ]
        return [candidate.resolve(strict=False) for candidate in candidates if candidate]

    def locate_executable(self, name: str) -> Path | None:
        env_name = f"ROTOWEAVE_{name.upper()}"
        candidates: list[Path] = []
        if compatible_environment_value(env_name):
            candidates.append(Path(os.environ[env_name]))

        if name.lower() == "ffmpeg":
            candidates.extend(
                [
                    self.runtime_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
                    self.runtime_root
                    / "dist"
                    / "RotoWeave"
                    / "_internal"
                    / "tools"
                    / "ffmpeg"
                    / "bin"
                    / "ffmpeg.exe",
                    self.runtime_root
                    / "release"
                    / "tools"
                    / "ffmpeg"
                    / "bin"
                    / "ffmpeg.exe",
                ]
            )
        elif name.lower() == "ffprobe":
            candidates.extend(
                [
                    self.runtime_root / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
                    self.runtime_root / "tools" / "ffprobe.exe",
                    self.runtime_root
                    / "dist"
                    / "RotoWeave"
                    / "_internal"
                    / "tools"
                    / "ffmpeg"
                    / "bin"
                    / "ffprobe.exe",
                    self.runtime_root
                    / "release"
                    / "tools"
                    / "ffmpeg"
                    / "bin"
                    / "ffprobe.exe",
                    self.runtime_root / "release" / "tools" / "ffprobe.exe",
                ]
            )

        path_value = compatible_environment_value("PATH", "")
        for folder in path_value.split(os.pathsep):
            if folder:
                candidates.append(Path(folder) / f"{name}.exe")
                candidates.append(Path(folder) / name)

        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        return None

    @property
    def birefnet_available(self) -> bool:
        return self.bundled_birefnet_onnx.is_file()

    @property
    def birefnet_mode(self) -> str:
        return "bundled-onnx" if self.birefnet_available else "chroma-only"


settings = Settings()
