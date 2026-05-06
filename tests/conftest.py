"""
--------------------------------------------------------------------------------
FILE:        conftest.py
PATH:        ~/projects/agent-broker/tests/conftest.py
DESCRIPTION: Shared pytest fixtures for isolated Agent Broker config, database, API, and MCP tests.

CHANGELOG:
2026-05-06 14:01      Codex      [Refactor] Rename operator permanently hidden thread fixture path and add probe auth secret.
2026-05-06 13:05      Codex      [Refactor] Add explicit probe flags and operator transcript config to isolated fixtures.
2026-05-06 09:47      Codex      [Refactor] Add explicit peer participant fields to isolated agent registry fixtures.
2026-05-06 09:11      Codex      [Refactor] Expand isolated test env fixtures for Phase 3 MCP and Origin coverage.
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass
class DataLayer:
    """Holds the freshly reloaded config + database modules for one test."""

    config: object
    database: object


def _reload_data_layer() -> tuple[object, object]:
    """Drop any cached agent_broker modules so config picks up env vars."""
    for name in sorted(sys.modules, reverse=True):
        if name == "agent_broker" or name.startswith("agent_broker."):
            sys.modules.pop(name, None)
    config = importlib.import_module("agent_broker.config")
    database = importlib.import_module("agent_broker.database")
    return config, database


@pytest.fixture
def data_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DataLayer]:
    """Provide a fresh agents.json + on-disk SQLite + reloaded modules."""
    agents_path = tmp_path / "agents.json"
    agents_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "prod-claude",
                        "host": "prod",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "a" * 64,
                    },
                    {
                        "agent_id": "prod-codex",
                        "host": "prod",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "b" * 64,
                    },
                    {
                        "agent_id": "dev-claude",
                        "host": "dev",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "c" * 64,
                    },
                    {
                        "agent_id": "dev-codex",
                        "host": "dev",
                        "role": "executor",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "d" * 64,
                    },
                    {
                        "agent_id": "flow-claude",
                        "host": "flow",
                        "role": "arbitrator",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "e" * 64,
                    },
                    {
                        "agent_id": "operator",
                        "host": "pi",
                        "role": "operator",
                        "participant_type": "operator",
                        "transport_type": "http",
                        "is_probe": False,
                        "secret": "g" * 64,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "broker.sqlite"
    permanently_hidden_threads_path = tmp_path / "operator_permanently_hidden_threads.json"
    _write_permanently_hidden_threads(permanently_hidden_threads_path)
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_AGENT_SECRETS_PATH", str(tmp_path / "agent-secrets.json"))
    monkeypatch.setenv(
        "OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH", str(permanently_hidden_threads_path)
    )
    monkeypatch.setenv("OPERATOR_PARTICIPANT_ID", "operator")
    monkeypatch.setenv("DELPHI_DB_PATH", str(db_path))
    monkeypatch.setenv("DELPHI_HOST", "127.0.0.1")
    monkeypatch.setenv("DELPHI_PORT", "8420")
    monkeypatch.setenv("DELPHI_MCP_HOST_REGISTRY", "127.0.0.1:*,localhost:*")
    monkeypatch.setenv(
        "DELPHI_MCP_ORIGIN_REGISTRY",
        "http://127.0.0.1:8420,http://localhost:8420",
    )
    monkeypatch.setenv("DELPHI_WEB_SECURE", "false")
    monkeypatch.setenv("DELPHI_NUDGE_SWEEP_ENABLED", "false")
    monkeypatch.setenv("DELPHI_MCP_SESSION_MANAGER_ENABLED", "false")
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "dev-codex")

    config, database = _reload_data_layer()
    yield DataLayer(config=config, database=database)


@pytest.fixture
def conn(data_layer: DataLayer) -> Iterator[sqlite3.Connection]:
    """Open a fresh connection (init runs on first call) and close on teardown."""
    connection = data_layer.database.get_connection(data_layer.config.DB_PATH)
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# v2 API / MCP fixtures
# ---------------------------------------------------------------------------


_API_OPERATOR_TOKEN = "test-operator-token"


# Per-agent test secrets used by the API + MCP fixtures. These mirror what
# the agents.json fixtures inject (one char repeated 64 times).
_AGENT_SECRETS_FIXTURE: dict[str, str] = {
    "prod-claude": "a" * 64,
    "prod-codex": "b" * 64,
    "dev-claude": "c" * 64,
    "dev-codex": "d" * 64,
    "flow-claude": "e" * 64,
    "exec-codex": "f" * 64,
    "operator": "g" * 64,
    "pi-claude-probe": "h" * 64,
    "pi-codex-probe": "i" * 64,
    "prod-probe": "j" * 64,
}


@dataclass
class ApiHarness:
    """Bundle of reloaded modules + TestClient + workflow connection helper."""

    config: object
    database: object
    workflow: object
    mcp_server: object
    app: object
    client: object


def _reload_full_stack() -> tuple[object, object, object, object, object]:
    for name in sorted(sys.modules, reverse=True):
        if name == "agent_broker" or name.startswith("agent_broker."):
            sys.modules.pop(name, None)
    config = importlib.import_module("agent_broker.config")
    database = importlib.import_module("agent_broker.database")
    workflow = importlib.import_module("agent_broker.workflow")
    mcp_server = importlib.import_module("agent_broker.mcp_server")
    main = importlib.import_module("agent_broker.main")
    return config, database, workflow, mcp_server, main


def _write_permanently_hidden_threads(path: Path, thread_ids: tuple[str, ...] = ()) -> None:
    path.write_text(
        json.dumps(
            {
                "_meta": {"changelog": []},
                "thread_ids": list(thread_ids),
            }
        ),
        encoding="utf-8",
    )


def _write_full_agents(agents_path: Path) -> None:
    """Two workers per host (prod + dev), arbitrator, and executor."""
    agents_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "prod-claude",
                        "host": "prod",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["prod-claude"],
                    },
                    {
                        "agent_id": "prod-codex",
                        "host": "prod",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["prod-codex"],
                    },
                    {
                        "agent_id": "dev-claude",
                        "host": "dev",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["dev-claude"],
                    },
                    {
                        "agent_id": "dev-codex",
                        "host": "dev",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["dev-codex"],
                    },
                    {
                        "agent_id": "flow-claude",
                        "host": "flow",
                        "role": "arbitrator",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["flow-claude"],
                    },
                    {
                        "agent_id": "exec-codex",
                        "host": "exec",
                        "role": "executor",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["exec-codex"],
                    },
                    {
                        "agent_id": "operator",
                        "host": "pi",
                        "role": "operator",
                        "participant_type": "operator",
                        "transport_type": "http",
                        "is_probe": False,
                        "secret": _AGENT_SECRETS_FIXTURE["operator"],
                    },
                    {
                        "agent_id": "prod-probe",
                        "host": "prod",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": True,
                        "secret": _AGENT_SECRETS_FIXTURE["prod-probe"],
                    },
                    {
                        "agent_id": "pi-claude-probe",
                        "host": "pi",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "http",
                        "is_probe": True,
                        "secret": _AGENT_SECRETS_FIXTURE["pi-claude-probe"],
                    },
                    {
                        "agent_id": "pi-codex-probe",
                        "host": "pi",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "http",
                        "is_probe": True,
                        "secret": _AGENT_SECRETS_FIXTURE["pi-codex-probe"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def api_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiHarness]:
    """Reloaded agent_broker stack with a TestClient for the v2 API."""
    from fastapi.testclient import TestClient

    agents_path = tmp_path / "agents.json"
    _write_full_agents(agents_path)
    db_path = tmp_path / "broker.sqlite"
    permanently_hidden_threads_path = tmp_path / "operator_permanently_hidden_threads.json"
    _write_permanently_hidden_threads(permanently_hidden_threads_path)
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_AGENT_SECRETS_PATH", str(tmp_path / "agent-secrets.json"))
    monkeypatch.setenv(
        "OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH", str(permanently_hidden_threads_path)
    )
    monkeypatch.setenv("OPERATOR_PARTICIPANT_ID", "operator")
    monkeypatch.setenv("DELPHI_DB_PATH", str(db_path))
    monkeypatch.setenv("DELPHI_HOST", "127.0.0.1")
    monkeypatch.setenv("DELPHI_PORT", "8420")
    monkeypatch.setenv("DELPHI_MCP_HOST_REGISTRY", "127.0.0.1:*,localhost:*")
    monkeypatch.setenv(
        "DELPHI_MCP_ORIGIN_REGISTRY",
        "http://127.0.0.1:8420,http://localhost:8420",
    )
    monkeypatch.setenv("DELPHI_WEB_SECURE", "false")
    monkeypatch.setenv("DELPHI_NUDGE_SWEEP_ENABLED", "false")
    monkeypatch.setenv("DELPHI_MCP_SESSION_MANAGER_ENABLED", "false")
    monkeypatch.setenv("DELPHI_OPERATOR_TOKEN", _API_OPERATOR_TOKEN)
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "exec-codex")
    config, database, workflow, mcp_server, main = _reload_full_stack()

    client = TestClient(main.app)
    client.headers.update({"X-Operator-Token": _API_OPERATOR_TOKEN})
    client.headers.update({"Origin": "http://127.0.0.1:8420"})
    try:
        yield ApiHarness(
            config=config,
            database=database,
            workflow=workflow,
            mcp_server=mcp_server,
            app=main.app,
            client=client,
        )
    finally:
        client.close()


@pytest.fixture
def client(api_harness: ApiHarness):
    """FastAPI TestClient pre-configured with the operator token."""
    return api_harness.client


@pytest.fixture
def operator_token() -> str:
    return _API_OPERATOR_TOKEN


@pytest.fixture
def agent_secrets() -> dict[str, str]:
    """The per-agent HMAC secrets used in test fixtures."""
    return dict(_AGENT_SECRETS_FIXTURE)


@pytest.fixture
def signed_request(api_harness: ApiHarness, agent_secrets: dict[str, str]):
    """Helper to compute the HMAC signature for a pre-built field tuple.

    Usage::

        sig = signed_request(
            agent_id='prod-claude',
            fields=database.build_emit_response_signature_fields(...),
        )

    Tests are responsible for choosing the timestamp embedded in `fields`;
    that gives them full control over freshness/staleness scenarios.
    """
    database = api_harness.database

    def _make(agent_id: str, fields: tuple[str, ...]) -> str:
        secret = agent_secrets[agent_id]
        return database.compute_signature(secret, *fields)

    return _make
