"""
--------------------------------------------------------------------------------
FILE:        test_transport_policy.py
PATH:        ~/projects/agent-broker/tests/test_transport_policy.py
DESCRIPTION: Unit tests for deterministic transport Origin and loopback policy helpers.

CHANGELOG:
2026-05-06 08:30      Codex      [Feature] Add Phase 1 transport policy and regression coverage for MCP ingress.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import pytest

from agent_broker.transport_policy import (
    TransportPolicy,
    _is_loopback_host,
    validate_origin,
)


def _policy() -> TransportPolicy:
    return TransportPolicy(
        origin_registry=("http://127.0.0.1:8420", "http://localhost:8420"),
    )


def test_missing_origin_allowed_for_loopback_client():
    allowed, reason = validate_origin(
        policy=_policy(),
        client_host="127.0.0.1",
        origin=None,
    )
    assert allowed is True
    assert reason is None


def test_missing_origin_rejects_spoofed_loopback_host_header():
    allowed, reason = validate_origin(
        policy=_policy(),
        client_host="203.0.113.1",
        origin=None,
    )
    assert allowed is False
    assert reason == "missing Origin is allowed only on loopback ingress"


def test_missing_origin_rejected_for_non_loopback_ingress():
    allowed, reason = validate_origin(
        policy=_policy(),
        client_host="192.0.2.10",
        origin=None,
    )
    assert allowed is False
    assert reason == "missing Origin is allowed only on loopback ingress"


def test_registered_origin_allowed_for_non_loopback_client():
    allowed, reason = validate_origin(
        policy=_policy(),
        client_host="192.0.2.10",
        origin="http://127.0.0.1:8420",
    )
    assert allowed is True
    assert reason is None


def test_unregistered_origin_rejected():
    allowed, reason = validate_origin(
        policy=_policy(),
        client_host="127.0.0.1",
        origin="http://example.test:8420",
    )
    assert allowed is False
    assert reason == "Origin is not registered for this broker"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.1:8420", True),
        ("localhost", True),
        ("localhost:8420", True),
        ("::1", True),
        ("[::1]", True),
        ("[::1]:8420", True),
        ("::1:8420", False),
        ("203.0.113.1", False),
        ("evil.example.com:8420", False),
        (None, False),
        ("", False),
    ],
)
def test_loopback_host_parsing(host, expected):
    assert _is_loopback_host(host) is expected
