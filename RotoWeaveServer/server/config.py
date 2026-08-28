from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import errno
import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.product import PRODUCT_VERSION
from contracts.brand_migration import migrate_server_local_app_data


LAUNCHER_CONFIG_FILE_NAME = "server-launcher.json"
TRUSTED_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class NetworkSettingsError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def normalize_api_host(value: object) -> str:
    normalized = str(value or "").strip()
    if normalized == "localhost":
        normalized = "127.0.0.1"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise NetworkSettingsError("invalid_api_host", "apiHost 必须是固定的 IPv4 地址。") from exc
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or not (
            address.is_loopback
            or any(address in network for network in TRUSTED_LAN_NETWORKS)
        )
    ):
        raise NetworkSettingsError(
            "invalid_api_host",
            "apiHost 只能使用本机回环或 RFC1918 私有局域网 IPv4 地址。",
        )
    return str(address)


def validate_api_port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise NetworkSettingsError("invalid_api_port", "apiPort 必须位于 1024–65535。")
    return value


def _private_lan_ipv4(value: object) -> str | None:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address) or address.is_loopback:
        return None
    if not any(address in network for network in TRUSTED_LAN_NETWORKS):
        return None
    return str(address)


def _default_route_ipv4() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect only asks the OS routing table for the preferred source
        # address; it does not send traffic to the documentation-only address.
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def _hostname_ipv4_candidates() -> list[str]:
    try:
        records = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    return [str(record[4][0]) for record in records]


def _host_is_bindable(host: str) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def discover_lan_ipv4() -> str:
    candidates: list[str] = []
    route_candidate = _private_lan_ipv4(_default_route_ipv4())
    if route_candidate:
        candidates.append(route_candidate)
    candidates.extend(
        candidate
        for value in _hostname_ipv4_candidates()
        if (candidate := _private_lan_ipv4(value)) is not None
    )
    unique = list(dict.fromkeys(candidates))
    if route_candidate is None:
        unique.sort(
            key=lambda value: (
                0 if value.startswith("192.168.") else 1 if value.startswith("10.") else 2,
                ipaddress.ip_address(value),
            )
        )
    for candidate in unique:
        if _host_is_bindable(candidate):
            return candidate
    raise NetworkSettingsError(
        "lan_address_unavailable",
        "未检测到可绑定的本机局域网 IPv4，请检查服务器网卡和默认路由。",
        status_code=409,
    )


def default_launcher_config() -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "productVersion": PRODUCT_VERSION,
        "apiHost": "127.0.0.1",
        "apiPort": 8443,
        "adminPort": 8444,
        "ttlHours": 24,
        "openAdminPage": True,
        "logRetentionDays": 30,
        "logMaxRows": 100000,
    }


def _assert_endpoint_available(host: str, port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
    except OSError as exc:
        error_number = exc.winerror if getattr(exc, "winerror", None) is not None else exc.errno
        if error_number in {errno.EADDRNOTAVAIL, 10049}:
            raise NetworkSettingsError(
                "api_host_unavailable",
                f"当前服务器无法绑定地址 {host}，请选择本机实际使用的固定局域网 IPv4。",
                status_code=409,
            ) from exc
        raise NetworkSettingsError(
            "api_port_unavailable",
            f"地址 {host}:{port} 当前不可用或端口已被占用，请选择其他端口。",
            status_code=409,
        ) from exc
    finally:
        probe.close()


def _default_root() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_REMOTE_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = compatible_environment_value("LOCALAPPDATA")
    if local_app_data:
        return migrate_server_local_app_data(Path(local_app_data)).resolve(strict=False)
    return (Path.cwd() / "remote-service-data").resolve()


@dataclass(slots=True)
class RemoteServerSettings:
    data_root: Path = field(default_factory=_default_root)
    api_host: str = field(
        default_factory=lambda: compatible_environment_value("ROTOWEAVE_REMOTE_HOST", "127.0.0.1")
    )
    api_port: int = field(
        default_factory=lambda: int(compatible_environment_value("ROTOWEAVE_REMOTE_PORT", "8443"))
    )
    admin_host: str = "127.0.0.1"
    admin_port: int = field(
        default_factory=lambda: int(compatible_environment_value("ROTOWEAVE_REMOTE_ADMIN_PORT", "8444"))
    )
    launcher_config_path: Path | None = None
    ttl_hours: float = field(
        default_factory=lambda: float(compatible_environment_value("ROTOWEAVE_REMOTE_TTL_HOURS", "24"))
    )
    max_upload_bytes: int = 32 * 1024 * 1024 * 1024
    log_retention_days: int = field(
        default_factory=lambda: int(compatible_environment_value("ROTOWEAVE_LOG_RETENTION_DAYS", "30"))
    )
    log_max_rows: int = field(
        default_factory=lambda: int(compatible_environment_value("ROTOWEAVE_LOG_MAX_ROWS", "100000"))
    )
    log_file_max_bytes: int = 20 * 1024 * 1024
    log_file_backups: int = 5

    def __post_init__(self) -> None:
        self.data_root = self.data_root.resolve(strict=False)
        self.api_host = normalize_api_host(self.api_host)
        configured_launcher = compatible_environment_value("ROTOWEAVE_SERVER_LAUNCHER_CONFIG")
        if self.launcher_config_path is None:
            self.launcher_config_path = (
                Path(os.path.expandvars(configured_launcher)).expanduser().resolve(strict=False)
                if configured_launcher
                else self.data_root / LAUNCHER_CONFIG_FILE_NAME
            )
        else:
            self.launcher_config_path = self.launcher_config_path.expanduser().resolve(strict=False)
        if self.admin_host != "127.0.0.1":
            raise ValueError("The remote administration site must bind to 127.0.0.1.")
        if (
            not 1024 <= self.api_port <= 65535
            or not 1024 <= self.admin_port <= 65535
            or self.api_port == self.admin_port
            or self.ttl_hours < 0
            or self.max_upload_bytes <= 0
            or not 1 <= self.log_retention_days <= 365
            or self.log_max_rows <= 0
        ):
            raise ValueError("Remote result TTL or upload limit is invalid.")

    def _read_launcher_payload(self) -> dict[str, Any]:
        assert self.launcher_config_path is not None
        if not self.launcher_config_path.is_file():
            return {
                **default_launcher_config(),
                "apiHost": self.api_host,
                "apiPort": self.api_port,
                "adminPort": self.admin_port,
            }
        try:
            payload = json.loads(self.launcher_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NetworkSettingsError("launcher_config_invalid", "服务端启动配置文件无法读取或不是有效 JSON。", status_code=409) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != 3
            or payload.get("productVersion") != PRODUCT_VERSION
        ):
            raise NetworkSettingsError(
                "launcher_config_invalid",
                f"服务端启动配置必须是 RotoWeave {PRODUCT_VERSION} schema 3。",
                status_code=409,
            )
        return payload

    def network_status(self) -> dict[str, Any]:
        configuration_error: str | None = None
        try:
            payload = self._read_launcher_payload()
            configured_port = validate_api_port(payload.get("apiPort"))
        except NetworkSettingsError as exc:
            configured_port = self.api_port
            configuration_error = str(exc)
        address_error: str | None = None
        try:
            service_host = discover_lan_ipv4()
        except NetworkSettingsError as exc:
            service_host = ""
            address_error = str(exc)
        configured_host = service_host or self.api_host
        endpoint = f"http://{self.api_host}:{self.api_port}"
        configured_endpoint = f"http://{configured_host}:{configured_port}"
        return {
            "service_host": service_host,
            "service_endpoint": configured_endpoint if service_host else "",
            "api_host": self.api_host,
            "api_port": self.api_port,
            "endpoint": endpoint,
            "api_path": "/api/matting/v1",
            "scope": "loopback" if self.api_host == "127.0.0.1" else "trusted-lan",
            "loopback_only": self.api_host == "127.0.0.1",
            "admin_host": self.admin_host,
            "admin_port": self.admin_port,
            "admin_endpoint": f"http://{self.admin_host}:{self.admin_port}",
            "configured_host": configured_host,
            "configured_port": configured_port,
            "configured_endpoint": configured_endpoint,
            "restart_required": (configured_host, configured_port) != (self.api_host, self.api_port),
            "configuration_error": configuration_error,
            "address_error": address_error,
        }

    def save_network_settings(self, api_port: object) -> dict[str, Any]:
        host = discover_lan_ipv4()
        port = validate_api_port(api_port)
        if port == self.admin_port:
            raise NetworkSettingsError(
                "api_port_conflict",
                f"远程 API 端口不能与本机管理端口 {self.admin_port} 相同。",
                status_code=409,
            )
        payload = self._read_launcher_payload()
        if (host, port) != (self.api_host, self.api_port):
            _assert_endpoint_available(host, port)
        payload["apiHost"] = host
        payload["apiPort"] = port
        assert self.launcher_config_path is not None
        self.launcher_config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.launcher_config_path.with_suffix(self.launcher_config_path.suffix + ".network.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.launcher_config_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise NetworkSettingsError("launcher_config_write_failed", "服务端启动配置保存失败，原配置保持不变。", status_code=409) from exc
        return self.network_status()

    @property
    def database_path(self) -> Path:
        return self.data_root / "queue.sqlite3"

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"

    @property
    def logs_root(self) -> Path:
        return self.data_root / "logs"

    def ensure_directories(self) -> None:
        for path in (
            self.data_root,
            self.jobs_root,
            self.logs_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
