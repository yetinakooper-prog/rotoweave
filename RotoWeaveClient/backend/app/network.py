from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]").split("%", 1)[0]
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def origin_is_allowed(origin: str | None, port: int) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if is_loopback_host(host):
        return origin_port in {port, 3000}
    return False
