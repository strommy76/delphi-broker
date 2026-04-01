"""
--------------------------------------------------------------------------------
FILE:        config.py
PATH:        C:/Projects/delphi-broker/src/delphi_broker/config.py
DESCRIPTION: Single import point for all configuration. Infrastructure from
             .env, agent registry from config/agents.json. Fail-loud on
             missing or invalid configuration.

CHANGELOG:
2026-03-31 17:30      Claude      [Header] Add file header
2026-03-31 16:30      Claude      [Harden] Fail-loud config, AGENT_SECRETS lookup
--------------------------------------------------------------------------------
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
WEB_UI_PASSWORD: str = os.environ.get("DELPHI_WEB_UI_PASSWORD", "").strip()

if not WEB_UI_PASSWORD:
    raise ValueError(
        "Missing DELPHI_WEB_UI_PASSWORD in environment/.env. "
        "The web approval surface requires explicit application-layer credentials."
    )

# ---------------------------------------------------------------------------
# Agent registry (from config/agents.json)
# ---------------------------------------------------------------------------
_agents_path_raw = os.environ.get("DELPHI_AGENTS_PATH", "config/agents.json")
_agents_file = (
    Path(_agents_path_raw)
    if Path(_agents_path_raw).is_absolute()
    else _PROJECT_ROOT / _agents_path_raw
)
if not _agents_file.exists():
    raise FileNotFoundError(
        f"Agent registry not found: {_agents_file}. "
        "Copy config/agents.json.example to config/agents.json and configure."
    )

_agents_data = json.loads(_agents_file.read_text())
SEED_AGENTS: list[dict] = _agents_data.get("agents", [])

# HMAC secret lookup: agent_id -> secret (for message signature verification)
AGENT_SECRETS: dict[str, str] = {}
for _agent in SEED_AGENTS:
    _secret = _agent.get("secret", "")
    if not _secret or _secret.startswith("GENERATE_WITH"):
        raise ValueError(
            f"Agent '{_agent['agent_id']}' has no valid secret in {_agents_file}. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    AGENT_SECRETS[_agent["agent_id"]] = _secret
