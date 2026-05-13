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
    accepted only for trusted ingress classes where the client address itself
    proves the private boundary: loopback clients and operator-declared CIDR
    ranges.
    """

    origin_registry: tuple[str, ...]
    originless_trusted_ingress_cidrs: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network,
        ...,
    ]


def _parse_ip_host(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if value is None or not value.strip():
        return None
    host = value.strip()
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_loopback_host(value: str | None) -> bool:
    parsed = _parse_ip_host(value)
    if parsed is not None:
        return parsed.is_loopback
    if value is None:
        return False
    host = value.strip()
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host == "localhost"


def _is_trusted_originless_ingress_host(
    value: str | None,
    trusted_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    parsed = _parse_ip_host(value)
    if parsed is None:
        return False
    return any(parsed in network for network in trusted_cidrs)


def validate_origin(
    *,
    policy: TransportPolicy,
    client_host: str | None,
    origin: str | None,
) -> tuple[bool, str | None]:
    """Return `(allowed, reason)` for an HTTP request origin boundary."""
    if origin is None or not origin.strip():
        if _is_loopback_host(client_host) or _is_trusted_originless_ingress_host(
            client_host,
            policy.originless_trusted_ingress_cidrs,
        ):
            return True, None
        return False, "missing Origin is allowed only on loopback or configured trusted ingress"

    parsed = urlparse(origin.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "Origin must be an absolute http(s) origin"
    normalized = f"{parsed.scheme}://{parsed.netloc}"
    if normalized not in policy.origin_registry:
        return False, "Origin is not registered for this broker"
    return True, None
