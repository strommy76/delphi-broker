"""Delphi Broker configuration."""

from __future__ import annotations

from pathlib import Path

HOST = "0.0.0.0"
PORT = 8420
DB_PATH = Path(__file__).resolve().parent.parent.parent / "delphi.db"

SEED_AGENTS = [
    {"agent_id": "dev-codex", "host": "desktop-vc94kou", "roles": "worker"},
    {"agent_id": "dev-claude", "host": "desktop-vc94kou", "roles": "worker"},
    {"agent_id": "prod-codex", "host": "bsalien", "roles": "worker,reviewer"},
    {"agent_id": "prod-claude", "host": "bsalien", "roles": "worker,reviewer"},
    {"agent_id": "bsflow-claude", "host": "bsflow", "roles": "worker,orchestrator"},
]

# Web UI always acts as orchestrator
WEB_UI_AGENT_ID = "web-ui"
WEB_UI_ROLES = "orchestrator"
