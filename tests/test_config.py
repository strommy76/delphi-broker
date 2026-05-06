"""
--------------------------------------------------------------------------------
FILE:        test_config.py
PATH:        ~/projects/agent-broker/tests/test_config.py
DESCRIPTION: Fail-loud configuration loader regression tests.

CHANGELOG:
2026-05-06 14:02      Codex      [Refactor] Rename operator permanently hidden thread config env fixture.
2026-05-06 13:12      Codex      [Refactor] Add required operator authority and excluded-thread config to fail-loud fixtures.
2026-05-06 09:47      Codex      [Refactor] Add coverage for required peer participant identity fields in agents.json.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest


def test_config_rejects_agent_missing_participant_identity(tmp_path, monkeypatch):
    agents_path = tmp_path / "agents.json"
    agents_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "pi-codex",
                        "host": "pi",
                        "role": "worker",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    permanently_hidden_threads_path = tmp_path / "operator_permanently_hidden_threads.json"
    permanently_hidden_threads_path.write_text(
        json.dumps({"_meta": {"changelog": []}, "thread_ids": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_AGENT_SECRETS_PATH", str(tmp_path / "agent-secrets.json"))
    monkeypatch.setenv(
        "OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH", str(permanently_hidden_threads_path)
    )
    monkeypatch.setenv("OPERATOR_PARTICIPANT_ID", "pi-codex")
    monkeypatch.setenv("DELPHI_DB_PATH", str(tmp_path / "broker.sqlite"))
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
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "pi-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "pi-codex")
    for name in sorted(sys.modules, reverse=True):
        if name == "agent_broker" or name.startswith("agent_broker."):
            sys.modules.pop(name, None)

    with pytest.raises(ValueError, match="participant_type"):
        importlib.import_module("agent_broker.config")
