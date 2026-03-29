"""Delphi Broker configuration.

SSOT: Infrastructure config comes from .env (or environment).
      Agent registry comes from config/agents.json.
      This module is the single import point for all configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Load .env if present (lightweight, no extra dependency)
# ---------------------------------------------------------------------------
_env_file = _PROJECT_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value

# ---------------------------------------------------------------------------
# Infrastructure (from environment / .env)
# ---------------------------------------------------------------------------
HOST: str = os.environ.get("DELPHI_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("DELPHI_PORT", "8420"))

_db_path_raw = os.environ.get("DELPHI_DB_PATH", "delphi.db")
DB_PATH: Path = (
    Path(_db_path_raw) if Path(_db_path_raw).is_absolute() else _PROJECT_ROOT / _db_path_raw
)

WEB_UI_AGENT_ID: str = os.environ.get("DELPHI_WEB_UI_AGENT_ID", "web-ui")
WEB_UI_ROLES: str = os.environ.get("DELPHI_WEB_UI_ROLES", "orchestrator")

# ---------------------------------------------------------------------------
# Agent registry (from config/agents.json)
# ---------------------------------------------------------------------------
_agents_file = _PROJECT_ROOT / "config" / "agents.json"
if _agents_file.exists():
    _agents_data = json.loads(_agents_file.read_text())
    SEED_AGENTS: list[dict] = _agents_data.get("agents", [])
else:
    SEED_AGENTS = []
