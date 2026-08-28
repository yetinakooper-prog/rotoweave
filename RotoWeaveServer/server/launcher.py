from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from contracts.paths import resolve_models_root
from contracts.brand_migration import migrate_server_local_app_data
from typing import Any

import uvicorn
from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:  # Setup installs tray support; keep diagnostic source runs usable.
    pystray = None  # type: ignore[assignment]

from contracts.product import PRODUCT_VERSION
from server.api import create_admin_app, create_remote_app
from server.config import (
    LAUNCHER_CONFIG_FILE_NAME,
    RemoteServerSettings,
    default_launcher_config,
    discover_lan_ipv4,
    normalize_api_host,
)
from server.service import RemoteService


CONFIG_FILE_NAME = LAUNCHER_CONFIG_FILE_NAME
SERVER_DATA_DIRECTORY = "RotoWeave-4.0-Server"
PID_FILE_NAME = "server.pid"


def data_root() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_REMOTE_DATA_ROOT")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve(strict=False)
    local_app_data = compatible_environment_value("LOCALAPPDATA")
    if local_app_data:
        return migrate_server_local_app_data(Path(local_app_data)).resolve(strict=False)
    return (Path.home() / f".{SERVER_DATA_DIRECTORY.lower()}").resolve(strict=False)


def config_path() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_SERVER_LAUNCHER_CONFIG")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve(strict=False)
    return data_root() / CONFIG_FILE_NAME


def pid_path(root: Path | None = None) -> Path:
    return (root or data_root()) / PID_FILE_NAME


def _port_is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _ensure_ports_available(connection: dict[str, Any]) -> None:
    endpoint = str(connection["endpoint"])
    api_host_port = endpoint.removeprefix("http://").rsplit(":", 1)
    occupied: list[str] = []
    if len(api_host_port) == 2 and _port_is_listening(api_host_port[0], int(api_host_port[1])):
        occupied.append(endpoint)
    admin = str(connection["admin"])
    admin_port = int(admin.rsplit(":", 1)[1])
    if _port_is_listening("127.0.0.1", admin_port):
        occupied.append(admin)
    if occupied:
        addresses = "、".join(occupied)
        raise RuntimeError(
            f"服务端口已被占用：{addresses}。如果 RotoWeave 已在运行，请先执行 "
            "Stop.cmd，再重新启动。"
        )


def _write_pid_marker(path: Path) -> None:
    payload = {
        "schemaVersion": 1,
        "productVersion": PRODUCT_VERSION,
        "pid": os.getpid(),
        "startedAtUtc": datetime.now(timezone.utc).isoformat(),
        "executable": str(Path(sys.executable).resolve()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _remove_owned_pid_marker(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("pid") == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        return


def default_config() -> dict[str, Any]:
    return default_launcher_config()


def ensure_config(path: Path | None = None) -> Path:
    target = (path or config_path()).resolve(strict=False)
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def _integer(payload: dict[str, Any], field: str, minimum: int, maximum: int) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须位于 {minimum}–{maximum}。")
    return value


def _trusted_lan_host(value: object) -> str:
    return normalize_api_host(value)


def apply_config(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    target = ensure_config(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"服务端启动配置无效：{target}") from exc
    if payload.get("schemaVersion") != 3 or payload.get("productVersion") != PRODUCT_VERSION:
        raise ValueError(f"服务端启动配置只支持 RotoWeave {PRODUCT_VERSION}：{target}")
    unexpected = sorted(set(payload) - set(default_config()))
    if unexpected:
        raise ValueError(f"服务端启动配置包含非当前字段：{'、'.join(unexpected)}")
    network_warning: str | None = None
    try:
        host = discover_lan_ipv4()
    except ValueError as exc:
        host = "127.0.0.1"
        network_warning = str(exc)
    if payload.get("apiHost") != host:
        payload["apiHost"] = host
        temporary = target.with_suffix(target.suffix + ".network.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
    api_port = _integer(payload, "apiPort", 1024, 65535)
    admin_port = _integer(payload, "adminPort", 1024, 65535)
    if api_port == admin_port:
        raise ValueError("apiPort 与 adminPort 不能相同。")
    ttl = payload.get("ttlHours")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 <= float(ttl) <= 24 * 30:
        raise ValueError("ttlHours 必须位于 0–720。")
    retention = _integer(payload, "logRetentionDays", 1, 365)
    max_rows = _integer(payload, "logMaxRows", 1000, 10_000_000)
    root = data_root()
    os.environ["ROTOWEAVE_REMOTE_DATA_ROOT"] = str(root)
    os.environ["ROTOWEAVE_REMOTE_HOST"] = host
    os.environ["ROTOWEAVE_REMOTE_PORT"] = str(api_port)
    os.environ["ROTOWEAVE_REMOTE_ADMIN_PORT"] = str(admin_port)
    os.environ["ROTOWEAVE_REMOTE_TTL_HOURS"] = str(float(ttl))
    os.environ["ROTOWEAVE_LOG_RETENTION_DAYS"] = str(retention)
    os.environ["ROTOWEAVE_LOG_MAX_ROWS"] = str(max_rows)
    model_library = resolve_models_root(Path(__file__).resolve().parents[1]) / "library"
    connection = {
        "endpoint": f"http://{host}:{api_port}",
        "admin": f"http://127.0.0.1:{admin_port}",
        "modelLibrary": str(model_library.resolve(strict=False)),
        "openAdminPage": payload.get("openAdminPage") is True,
    }
    if network_warning:
        connection["networkWarning"] = network_warning
    return target, connection


def _configure_logging(root: Path) -> Path:
    path = root / "logs" / "launcher.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return path


def _console_message(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    try:
        print(message, file=stream, flush=True)
    except OSError:
        return


def _message(title: str, body: str, flags: int) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(0, body, title, flags))
    except (AttributeError, OSError):
        logging.warning("%s: %s", title, body)
        return 0


def _json_request(url: str, timeout: float = 0.7) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.load(response)
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return None


def _open_admin(url: str) -> bool:
    if compatible_environment_value("ROTOWEAVE_LAUNCHER_NO_BROWSER") == "1":
        return False
    try:
        opened = bool(webbrowser.open(url))
        if not opened and os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]
            opened = True
        if opened:
            logging.info("已打开服务端管理页：%s", url)
        else:
            logging.warning("无法请求系统打开服务端管理页：%s", url)
        return opened
    except OSError:
        logging.exception("打开服务端管理页失败：%s", url)
        return False


def _base_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill=(17, 27, 22, 255), outline=(74, 116, 99, 255), width=2)
    for top in (13, 27, 41):
        draw.rounded_rectangle((13, top, 51, top + 9), radius=3, fill=(37, 61, 51, 255))
        draw.ellipse((17, top + 3, 20, top + 6), fill=(91, 226, 175, 255))
        draw.line((26, top + 4, 45, top + 4), fill=(113, 148, 134, 255), width=2)
    return image


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


class ServerHost:
    def __init__(self, settings: RemoteServerSettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._service: RemoteService | None = None
        self._servers: list[uvicorn.Server] = []
        self._threads: list[threading.Thread] = []
        self._stopping = False
        self.last_error: str | None = None

    @property
    def alive(self) -> bool:
        with self._lock:
            return bool(self._threads) and all(thread.is_alive() for thread in self._threads)

    def _serve(self, server: uvicorn.Server, label: str) -> None:
        try:
            server.run()
        except BaseException as exc:  # Uvicorn can surface bind failures as SystemExit.
            with self._lock:
                if not self._stopping:
                    self.last_error = f"{label} 退出：{exc}"
            if not self._stopping:
                logging.exception("RotoWeave %s listener exited", label)

    def start(self, timeout_seconds: float = 20.0) -> None:
        with self._lock:
            if self._threads:
                return
            self.last_error = None
            self._stopping = False
            service = RemoteService(self.settings)
            service.start()
            self._service = service
            admin_server = uvicorn.Server(
                uvicorn.Config(
                    create_admin_app(service),
                    host=self.settings.admin_host,
                    port=self.settings.admin_port,
                    log_level="info",
                    access_log=False,
                    log_config=None,
                )
            )
            remote_server = uvicorn.Server(
                uvicorn.Config(
                    create_remote_app(self.settings, service=service, manage_lifecycle=False),
                    host=self.settings.api_host,
                    port=self.settings.api_port,
                    log_level="info",
                    access_log=False,
                    log_config=None,
                )
            )
            self._servers = [admin_server, remote_server]
            self._threads = [
                threading.Thread(
                    target=self._serve,
                    args=(admin_server, "localhost 管理服务"),
                    name="rotoweave-server-admin",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._serve,
                    args=(remote_server, "LAN API"),
                    name="rotoweave-server-api",
                    daemon=True,
                ),
            ]
            for thread in self._threads:
                thread.start()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if (
                _port_is_listening(self.settings.admin_host, self.settings.admin_port)
                and _port_is_listening(self.settings.api_host, self.settings.api_port)
            ):
                return
            if not self.alive:
                break
            time.sleep(0.05)
        detail = self.last_error or "服务端口未能在限定时间内就绪。"
        self.stop()
        raise RuntimeError(detail)

    def stop(self) -> None:
        with self._lock:
            if not self._threads and self._service is None:
                return
            self._stopping = True
            servers = list(self._servers)
            threads = list(self._threads)
            service = self._service
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=12)
        for server, thread in zip(servers, threads, strict=True):
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=3)
        if service is not None:
            service.stop()
        with self._lock:
            self._servers = []
            self._threads = []
            self._service = None
            self._stopping = False


def _service_snapshot(admin_url: str, host: ServerHost) -> dict[str, Any]:
    payload = _json_request(f"{admin_url}/api/admin/v2/overview")
    if payload is None:
        if host.alive:
            return {"state": "warning", "active": 0, "label": "服务正在启动"}
        return {"state": "error", "active": 0, "label": "服务已停止"}
    if not host.alive:
        return {"state": "error", "active": 0, "label": "服务监听异常，请查看日志"}
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    states = queue.get("states") if isinstance(queue.get("states"), dict) else {}
    active = int(states.get("queued") or 0) + int(states.get("running") or 0)
    startup = payload.get("startup") if isinstance(payload.get("startup"), dict) else {}
    worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
    if startup.get("state") == "failed" or worker.get("state") == "error":
        return {"state": "error", "active": active, "label": "服务异常，请查看管理页"}
    if startup.get("state") != "ready":
        return {"state": "warning", "active": active, "label": "服务正在启动"}
    if active:
        return {"state": "processing", "active": active, "label": f"正在处理 {active} 个任务"}
    if worker.get("state") not in {"ready", "ready-with-warnings"}:
        return {"state": "warning", "active": 0, "label": "服务已启动，模型待激活"}
    return {"state": "idle", "active": 0, "label": "服务空闲"}


def _exit_from_tray(host: ServerHost, icon: Any, stopped: threading.Event, active: int) -> bool:
    if active:
        result = _message(
            "退出 RotoWeave Server",
            f"仍有 {active} 个任务正在处理或等待。\n\n退出会中断当前处理，任务将在下次启动时恢复。确定退出吗？",
            0x24,
        )
        if result != 6:
            return False
    stopped.set()
    host.stop()
    icon.stop()
    return True


def _run_with_tray(host: ServerHost, connection: dict[str, Any], log_path: Path) -> None:
    assert pystray is not None
    admin_url = str(connection["admin"])
    base_icon = _base_tray_image()
    snapshot_lock = threading.Lock()
    current = _service_snapshot(admin_url, host)
    stopped = threading.Event()

    def get_snapshot() -> dict[str, Any]:
        with snapshot_lock:
            return dict(current)

    def status_text(_: Any) -> str:
        return "状态：" + str(get_snapshot().get("label") or "检查中")

    def open_admin(_: Any = None, __: Any = None) -> None:
        _open_admin(admin_url)

    def exit_app(icon: Any, _: Any = None) -> None:
        _exit_from_tray(host, icon, stopped, int(get_snapshot().get("active") or 0))

    menu = pystray.Menu(
        pystray.MenuItem("打开管理中心", open_admin, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("打开日志", lambda *_: webbrowser.open(log_path.parent.as_uri())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出服务端", exit_app),
    )
    icon = pystray.Icon(
        "RotoWeaveServer",
        _status_icon(base_icon, str(current["state"])),
        f"RotoWeave Server · {current['label']}",
        menu,
    )

    def monitor() -> None:
        nonlocal current
        while not stopped.wait(1.0):
            snapshot = _service_snapshot(admin_url, host)
            if not host.alive and host.last_error:
                snapshot = {"state": "error", "active": 0, "label": "服务崩溃，请查看日志"}
            with snapshot_lock:
                changed = snapshot != current
                current = snapshot
            if changed:
                icon.icon = _status_icon(base_icon, str(snapshot["state"]))
                icon.title = f"RotoWeave Server · {snapshot['label']}"
                icon.update_menu()

    threading.Thread(target=monitor, name="rotoweave-server-tray-monitor", daemon=True).start()
    icon.run()


def _open_admin_when_ready(url: str) -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                if response.status == 200:
                    _open_admin(url)
                    return
        except Exception:
            time.sleep(0.1)


def main() -> None:
    root = data_root()
    log_path = _configure_logging(root)
    marker = pid_path(root)
    host: ServerHost | None = None
    try:
        path, connection = apply_config()
        _ensure_ports_available(connection)
        settings = RemoteServerSettings()
        host = ServerHost(settings)
        host.start()
        _write_pid_marker(marker)
        startup_messages = (
            f"RotoWeave {PRODUCT_VERSION} 远程服务",
            f"远程 API：{connection['endpoint']}/api/matting/v1",
            f"本机管理页：{connection['admin']}",
            "访问边界：可信局域网 HTTP，无客户端认证；请勿映射到公网。",
            f"独立模型库：{connection['modelLibrary']}",
            f"启动配置：{path}",
            f"启动日志：{log_path}",
            f"进程标记：{marker}",
        )
        for message in startup_messages:
            logging.info(message)
            _console_message(message)
        if connection["openAdminPage"] and compatible_environment_value("ROTOWEAVE_LAUNCHER_NO_BROWSER") != "1":
            threading.Thread(
                target=_open_admin_when_ready,
                args=(str(connection["admin"]),),
                name="admin-browser-opener",
                daemon=True,
            ).start()
        if pystray is None or compatible_environment_value("ROTOWEAVE_LAUNCHER_NO_TRAY") == "1":
            logging.warning("pystray is not installed; running without a system tray")
            while host.alive:
                time.sleep(0.5)
            if host.last_error:
                raise RuntimeError(host.last_error)
            return
        _run_with_tray(host, connection, log_path)
    except KeyboardInterrupt:
        return
    except Exception as exc:
        logging.exception("RotoWeave remote server failed to start")
        _console_message(f"启动失败：{exc}", error=True)
        _console_message(f"配置：{config_path()}", error=True)
        _console_message(f"日志：{log_path}", error=True)
        if getattr(sys, "frozen", False):
            _message(
                "RotoWeave Server 启动失败",
                f"{exc}\n\n请检查配置、端口和日志。\n\n日志：{log_path}",
                0x10,
            )
        raise SystemExit(1) from exc
    finally:
        if host is not None:
            host.stop()
        _remove_owned_pid_marker(marker)


if __name__ == "__main__":
    main()
