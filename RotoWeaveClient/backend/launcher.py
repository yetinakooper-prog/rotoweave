from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import ctypes
import atexit
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:  # Development environments may run the service without tray extras.
    pystray = None  # type: ignore[assignment]

from backend.app import __version__
from backend.app.config import settings
from contracts.product import HTTP_API_PREFIX, RUNTIME_SINGLE_INSTANCE_MUTEX


_ERROR_ALREADY_EXISTS = 183
_INSTANCE_MUTEX_NAME = RUNTIME_SINGLE_INSTANCE_MUTEX


def _url() -> str:
    return f"http://127.0.0.1:{settings.port}/"


def _json_request(path: str, authenticated: bool = False, timeout: float = 0.7) -> dict[str, Any] | list[Any] | None:
    headers = {"Accept": "application/json"}
    if authenticated:
        headers["X-RotoWeave-Session"] = settings.session_token
    try:
        request = urllib.request.Request(f"{_url()}{path.lstrip('/')}", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.load(response)
            return payload if isinstance(payload, (dict, list)) else None
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return None


def _running_version() -> str | None:
    payload = _json_request(f"{HTTP_API_PREFIX}/health")
    if not isinstance(payload, dict):
        return None
    return str(payload.get("version") or "unknown")


def _is_running() -> bool:
    return _running_version() == __version__


def _open_tool() -> None:
    if compatible_environment_value("ROTOWEAVE_LAUNCHER_NO_BROWSER") == "1":
        return
    bootstrap = settings.create_bootstrap_token()
    webbrowser.open(f"{_url()}?bootstrap={bootstrap}")


def _open_existing_tool() -> None:
    # A second process cannot mint a bootstrap token accepted by the already
    # running service. The browser already owns that service's HttpOnly cookie,
    # so reopening the stable local origin is the correct wake-up path.
    if compatible_environment_value("ROTOWEAVE_LAUNCHER_NO_BROWSER") != "1":
        webbrowser.open(_url())


def _open_when_ready(*, existing_instance: bool = False) -> None:
    for _ in range(120):
        if _is_running():
            if existing_instance:
                _open_existing_tool()
            else:
                _open_tool()
            return
        time.sleep(0.25)


def _configure_logging() -> Path:
    settings.ensure_directories()
    path = settings.local_state_root / "logs" / "service.log"
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    return path


def _message(title: str, body: str, flags: int) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(0, body, title, flags))
    except (AttributeError, OSError):
        logging.warning("%s: %s", title, body)
        return 0


class ServiceHost:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self, timeout_seconds: float = 12.0) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.last_error = None
            from backend.app.main import create_app

            config = uvicorn.Config(
                create_app(settings),
                host=settings.listen_host,
                port=settings.port,
                log_level="info",
                access_log=False,
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._serve, name="rotoweave-service", daemon=True)
            self._thread.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if _is_running():
                return
            if not self.alive:
                break
            time.sleep(0.05)
        detail = self.last_error or (
            f"本地端口 {settings.port} 无法启动或健康检查超时。"
        )
        self.stop()
        raise RuntimeError(detail)

    def _serve(self) -> None:
        try:
            assert self._server is not None
            self._server.run()
        except Exception as exc:  # pragma: no cover - exercised by packaged runtime.
            self.last_error = str(exc)
            logging.exception("RotoWeave service crashed")
        except SystemExit as exc:  # Uvicorn uses SystemExit for bind failures.
            self.last_error = f"服务进程退出（{exc.code}），端口可能已被占用。"
            logging.exception("RotoWeave service exited during startup")

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            if server:
                server.should_exit = True
        if thread and thread.is_alive():
            thread.join(timeout=12)
        with self._lock:
            self._server = None
            self._thread = None

    def restart(self) -> None:
        self.stop()
        self.start()


def _base_tray_image() -> Image.Image:
    candidates = [
        settings.runtime_root / "assets" / "tray-icon.png",
        settings.runtime_root / "release" / "tray-icon.png",
    ]
    for candidate in candidates:
        if candidate.is_file():
            with Image.open(candidate) as opened:
                return opened.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
    # Packaging should always include the real icon. This fallback keeps source runs usable.
    return Image.new("RGBA", (64, 64), (17, 27, 22, 255))


def _status_icon(base: Image.Image, state: str) -> Image.Image:
    colors = {
        "idle": (91, 226, 175, 255),
        "processing": (119, 184, 255, 255),
        "warning": (245, 203, 104, 255),
        "error": (255, 114, 114, 255),
    }
    image = base.copy()
    draw = ImageDraw.Draw(image)
    draw.ellipse((39, 39, 63, 63), fill=(8, 14, 11, 255))
    draw.ellipse((43, 43, 59, 59), fill=colors.get(state, colors["error"]))
    return image


def _service_snapshot() -> dict[str, Any]:
    health = _json_request(f"{HTTP_API_PREFIX}/health")
    if not isinstance(health, dict):
        return {"state": "error", "active": 0, "label": "服务未启动"}
    jobs_payload = _json_request(
        f"{HTTP_API_PREFIX}/jobs?limit=200", authenticated=True
    )
    jobs = jobs_payload if isinstance(jobs_payload, list) else []
    reviews_payload = _json_request(
        f"{HTTP_API_PREFIX}/reviews", authenticated=True
    )
    reviews = reviews_payload if isinstance(reviews_payload, list) else []
    active = sum(1 for job in jobs if isinstance(job, dict) and job.get("status") in {"queued", "running"})
    failed = sum(1 for job in jobs if isinstance(job, dict) and job.get("status") == "failed")
    if failed:
        return {"state": "error", "active": active, "label": f"{failed} 个任务失败"}
    if active:
        return {"state": "processing", "active": active, "label": f"正在处理 {active} 个任务"}
    if reviews:
        return {"state": "warning", "active": 0, "label": f"{len(reviews)} 个动画需审查"}
    return {"state": "idle", "active": 0, "label": "服务空闲"}


def _run_with_tray(host: ServiceHost, log_path: Path) -> None:
    assert pystray is not None
    base_icon = _base_tray_image()
    snapshot_lock = threading.Lock()
    current = _service_snapshot()
    stopped = threading.Event()

    def get_snapshot() -> dict[str, Any]:
        with snapshot_lock:
            return dict(current)

    def status_text(_: Any) -> str:
        return "状态：" + str(get_snapshot().get("label") or "检查中")

    def open_tool(_: Any = None, __: Any = None) -> None:
        _open_tool()

    def restart_service(_: Any = None, __: Any = None) -> None:
        host.restart()

    def exit_app(icon: Any, _: Any = None) -> None:
        snapshot = get_snapshot()
        active = int(snapshot.get("active") or 0)
        if active:
            result = _message(
                "退出 RotoWeave",
                f"仍有 {active} 个任务正在处理或等待。\n\n退出后任务会保存，下次启动将从最近检查点继续。确定退出吗？",
                0x24,
            )
            if result != 6:
                return
        stopped.set()
        host.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开工具", open_tool, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("重新启动服务", restart_service),
        pystray.MenuItem("打开日志", lambda *_: webbrowser.open(log_path.parent.as_uri())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", exit_app),
    )
    icon = pystray.Icon("RotoWeave", _status_icon(base_icon, "idle"), "RotoWeave", menu)

    def monitor() -> None:
        nonlocal current
        while not stopped.wait(1.0):
            snapshot = _service_snapshot()
            if not host.alive and snapshot.get("state") == "error" and host.last_error:
                snapshot["label"] = "服务崩溃，可从托盘重启"
            with snapshot_lock:
                changed = snapshot != current
                current = snapshot
            if changed:
                icon.icon = _status_icon(base_icon, str(snapshot["state"]))
                icon.title = f"RotoWeave · {snapshot['label']}"
                icon.update_menu()

    threading.Thread(target=monitor, name="rotoweave-tray-monitor", daemon=True).start()
    icon.run()


def main() -> None:
    mutex = None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        mutex = kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        already_running = kernel32.GetLastError() == _ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        already_running = False
    if mutex:
        atexit.register(kernel32.CloseHandle, mutex)
    if already_running:
        _open_when_ready(existing_instance=True)
        return
    running_version = _running_version()
    if running_version == __version__:
        _open_existing_tool()
        return
    if running_version is not None:
        _message(
            "RotoWeave 版本冲突",
            f"检测到 RotoWeave v{running_version} 仍在运行。\n\n请先从系统托盘退出旧版本，再启动 RotoWeave v{__version__}。",
            0x30,
        )
        return

    log_path = _configure_logging()

    host = ServiceHost()
    try:
        host.start()
    except RuntimeError as exc:
        logging.exception("RotoWeave local service failed to start")
        _message(
            "RotoWeave 启动失败",
            f"{exc}\n\n请检查端口 {settings.port}、日志和本机配置。\n\n日志：{log_path}",
            0x10,
        )
        return
    threading.Thread(target=_open_when_ready, name="browser-opener", daemon=True).start()
    if pystray is None or compatible_environment_value("ROTOWEAVE_LAUNCHER_NO_TRAY") == "1":
        logging.warning("pystray is not installed; running without a system tray")
        while host.alive:
            time.sleep(0.5)
        if host.last_error:
            _message("RotoWeave", f"RotoWeave 服务启动失败。\n\n{host.last_error}\n\n日志：{log_path}", 0x10)
        return
    try:
        _run_with_tray(host, log_path)
    finally:
        host.stop()


if __name__ == "__main__":
    main()
