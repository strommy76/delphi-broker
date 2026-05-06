"""
--------------------------------------------------------------------------------
FILE:        transport_policy.py
PATH:        ~/projects/agent-broker/src/agent_broker/transport_policy.py
DESCRIPTION: Deterministic HTTP Origin policy helpers for broker ingress.

CHANGELOG:
2026-05-06 08:30      Codex      [Feature] Add Phase 1 transport policy and regression coverage for MCP ingress.
--------------------------------------------------------------------------------

Transport boundary policy for broker HTTP/MCP ingress."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class TransportPolicy:
    """Deterministic HTTP ingress policy.

    `origin_registry` is loaded from environment config. Missing Origin is
    accepted only for loopback clients so native CLI MCP clients can operate
    without browser headers.
    """

    origin_registry: tuple[str, ...]


def _is_loopback_host(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    host = value.strip()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def validate_origin(
    *,
    policy: TransportPolicy,
    client_host: str | None,
    origin: str | None,
) -> tuple[bool, str | None]:
    """Return `(allowed, reason)` for an HTTP request origin boundary."""
    if origin is None or not origin.strip():
        if _is_loopback_host(client_host):
            return True, None
        return False, "missing Origin is allowed only on loopback ingress"

    parsed = urlparse(origin.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "Origin must be an absolute http(s) origin"
    normalized = f"{parsed.scheme}://{parsed.netloc}"
    if normalized not in policy.origin_registry:
        return False, "Origin is not registered for this broker"
    return True, None
