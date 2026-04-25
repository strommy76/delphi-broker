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
