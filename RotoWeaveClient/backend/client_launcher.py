from __future__ import annotations

from contracts.legacy_compat import compatible_environment_value

import json
import ipaddress
import os
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contracts.product import APPLICATION_DATA_DIRECTORY, PRODUCT_VERSION


CONFIG_FILE_NAME = "client-launcher.json"
_CONFIG_LOCK = threading.RLock()
_TRUSTED_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _config_root() -> Path:
    local_app_data = compatible_environment_value("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APPLICATION_DATA_DIRECTORY
    return Path.home() / f".{APPLICATION_DATA_DIRECTORY.lower()}"


def config_path() -> Path:
    configured = compatible_environment_value("ROTOWEAVE_CLIENT_LAUNCHER_CONFIG")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve(strict=False)
    return (_config_root() / CONFIG_FILE_NAME).resolve(strict=False)


def default_config() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "productVersion": PRODUCT_VERSION,
        "remoteMatting": {
            "enabled": False,
            "endpoint": "http://127.0.0.1:8443",
        },
    }


def ensure_config(path: Path | None = None) -> Path:
    target = (path or config_path()).resolve(strict=False)
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(default_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def _required_string(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空。")
    return text


def _load_config(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    target = ensure_config(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"客户端启动配置无效：{target}") from exc
    if payload.get("schemaVersion") != 2 or payload.get("productVersion") != PRODUCT_VERSION:
        raise ValueError(f"客户端启动配置只支持 RotoWeave {PRODUCT_VERSION}：{target}")
    unexpected = sorted(set(payload) - set(default_config()))
    if unexpected:
        raise ValueError(f"客户端启动配置包含非当前字段：{'、'.join(unexpected)}")
    remote = payload.get("remoteMatting")
    if not isinstance(remote, dict):
        raise ValueError("remoteMatting 必须是对象。")
    remote_unexpected = sorted(set(remote) - {"enabled", "endpoint"})
    if remote_unexpected:
        raise ValueError(
            f"remoteMatting 包含非当前字段：{'、'.join(remote_unexpected)}"
        )
    return target, payload


def remote_settings(path: Path | None = None) -> dict[str, Any]:
    target, payload = _load_config(path)
    remote = payload["remoteMatting"]
    endpoint = str(remote.get("endpoint") or "http://127.0.0.1:8443").strip()
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port or 8443
    except ValueError:
        port = 8443
    return {
        "enabled": remote.get("enabled") is True,
        "endpoint": endpoint.rstrip("/"),
        "host": parsed.hostname or "127.0.0.1",
        "port": port,
    }


def _normalized_endpoint(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as exc:
        raise ValueError("远程服务地址必须是固定局域网 IPv4。") from exc
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or not (
            address.is_loopback
            or any(address in network for network in _TRUSTED_LAN_NETWORKS)
        )
    ):
        raise ValueError("远程服务地址必须是回环或可信局域网 IPv4。")
    if isinstance(port, bool) or not 1024 <= int(port) <= 65535:
        raise ValueError("远程服务端口必须位于 1024–65535。")
    return f"http://{address}:{int(port)}"


def _validated_endpoint(value: object) -> str:
    endpoint = _required_string(value, "remoteMatting.endpoint")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/api/matting/v1"}
    ):
        raise ValueError("远程抠图 endpoint 必须是无内嵌凭据的可信 LAN HTTP 地址。")
    try:
        base = _normalized_endpoint(parsed.hostname, parsed.port or 8443)
    except ValueError as exc:
        raise ValueError(f"remoteMatting.endpoint 无效：{exc}") from exc
    return base + ("/api/matting/v1" if parsed.path.rstrip("/") else "")


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(value)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def _save_remote_settings_unlocked(
    *,
    enabled: bool,
    host: str,
    port: int,
    path: Path | None = None,
) -> dict[str, Any]:
    target, payload = _load_config(path)
    endpoint = _normalized_endpoint(host, port)
    payload["remoteMatting"] = {
        "enabled": bool(enabled),
        "endpoint": endpoint,
    }
    value = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    original = target.read_bytes() if target.is_file() else None
    try:
        _atomic_write(target, value)
        apply_config(target)
    except Exception:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, original)
        apply_config(target)
        raise
    return remote_settings(target)


def save_remote_settings(
    *,
    enabled: bool,
    host: str,
    port: int,
    path: Path | None = None,
) -> dict[str, Any]:
    with _CONFIG_LOCK:
        return _save_remote_settings_unlocked(
            enabled=enabled,
            host=host,
            port=port,
            path=path,
        )


def apply_config(path: Path | None = None) -> Path:
    target, payload = _load_config(path)
    remote = payload["remoteMatting"]
    if remote.get("enabled") is not True:
        for name in (
            "ROTOWEAVE_REMOTE_MATTING_URL",
            "ROTOWEAVE_REMOTE_MATTING_TOKEN",
            "ROTOWEAVE_REMOTE_MATTING_CA",
        ):
            os.environ.pop(name, None)
        return target

    endpoint = _validated_endpoint(remote.get("endpoint"))
    os.environ["ROTOWEAVE_REMOTE_MATTING_URL"] = endpoint.rstrip("/")
    os.environ.pop("ROTOWEAVE_REMOTE_MATTING_TOKEN", None)
    os.environ.pop("ROTOWEAVE_REMOTE_MATTING_CA", None)
    return target


def main() -> None:
    try:
        apply_config()
    except Exception as exc:
        if getattr(sys, "frozen", False):
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"{exc}\n\n配置文件：{config_path()}",
                "RotoWeave 4.0 客户端启动失败",
                0x10,
            )
            return
        raise
    from backend.launcher import main as launch_client

    launch_client()


if __name__ == "__main__":
    main()
