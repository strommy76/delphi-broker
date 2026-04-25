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
    """Drop any cached delphi_broker modules so config picks up env vars."""
    for name in sorted(sys.modules, reverse=True):
        if name == "delphi_broker" or name.startswith("delphi_broker."):
            sys.modules.pop(name, None)
    config = importlib.import_module("delphi_broker.config")
    database = importlib.import_module("delphi_broker.database")
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
                        "secret": "a" * 64,
                    },
                    {
                        "agent_id": "prod-codex",
                        "host": "prod",
                        "role": "worker",
                        "secret": "b" * 64,
                    },
                    {
                        "agent_id": "dev-claude",
                        "host": "dev",
                        "role": "worker",
                        "secret": "c" * 64,
                    },
                    {
                        "agent_id": "dev-codex",
                        "host": "dev",
                        "role": "executor",
                        "secret": "d" * 64,
                    },
                    {
                        "agent_id": "flow-claude",
                        "host": "flow",
                        "role": "arbitrator",
                        "secret": "e" * 64,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "broker.sqlite"
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_DB_PATH", str(db_path))

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
        if name == "delphi_broker" or name.startswith("delphi_broker."):
            sys.modules.pop(name, None)
    config = importlib.import_module("delphi_broker.config")
    database = importlib.import_module("delphi_broker.database")
    workflow = importlib.import_module("delphi_broker.workflow")
    mcp_server = importlib.import_module("delphi_broker.mcp_server")
    main = importlib.import_module("delphi_broker.main")
    return config, database, workflow, mcp_server, main


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
                        "secret": _AGENT_SECRETS_FIXTURE["prod-claude"],
                    },
                    {
                        "agent_id": "prod-codex",
                        "host": "prod",
                        "role": "worker",
                        "secret": _AGENT_SECRETS_FIXTURE["prod-codex"],
                    },
                    {
                        "agent_id": "dev-claude",
                        "host": "dev",
                        "role": "worker",
                        "secret": _AGENT_SECRETS_FIXTURE["dev-claude"],
                    },
                    {
                        "agent_id": "dev-codex",
                        "host": "dev",
                        "role": "worker",
                        "secret": _AGENT_SECRETS_FIXTURE["dev-codex"],
                    },
                    {
                        "agent_id": "flow-claude",
                        "host": "flow",
                        "role": "arbitrator",
                        "secret": _AGENT_SECRETS_FIXTURE["flow-claude"],
                    },
                    {
                        "agent_id": "exec-codex",
                        "host": "exec",
                        "role": "executor",
                        "secret": _AGENT_SECRETS_FIXTURE["exec-codex"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def api_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ApiHarness]:
    """Reloaded delphi_broker stack with a TestClient for the v2 API."""
    from fastapi.testclient import TestClient

    agents_path = tmp_path / "agents.json"
    _write_full_agents(agents_path)
    db_path = tmp_path / "broker.sqlite"
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_DB_PATH", str(db_path))
    monkeypatch.setenv("DELPHI_OPERATOR_TOKEN", _API_OPERATOR_TOKEN)
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "exec-codex")
    # Avoid an active background sweep task during synchronous tests.
    monkeypatch.setenv("DELPHI_DISABLE_NUDGE_SWEEP", "1")

    config, database, workflow, mcp_server, main = _reload_full_stack()

    client = TestClient(main.app)
    client.headers.update({"X-Operator-Token": _API_OPERATOR_TOKEN})
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
