"""Single import point for all configuration.

Infrastructure values come from the environment (or `.env` if present); the
agent registry is loaded from `config/agents.json`. Fail-loud on any missing
or invalid configuration — silent fallbacks are forbidden.
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
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#"):
            continue
        _key, _, _value = _line.partition("=")
        _key, _value = _key.strip(), _value.strip()
        if _key and _key not in os.environ:
            os.environ[_key] = _value

# ---------------------------------------------------------------------------
# Infrastructure (from environment / .env)
# ---------------------------------------------------------------------------
HOST: str = os.environ.get("DELPHI_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("DELPHI_PORT", "8420"))

_db_path_raw = os.environ.get("DELPHI_DB_PATH", "delphi.db")
DB_PATH: Path = (
    Path(_db_path_raw) if Path(_db_path_raw).is_absolute() else _PROJECT_ROOT / _db_path_raw
)

# Operator (web UI) authentication. Single secret in .env binds the
# session-creator. The web UI is no longer impersonating an agent identity.
WEB_UI_PASSWORD: str = os.environ.get("DELPHI_WEB_UI_PASSWORD", "").strip()

# Designated arbitrator and executor agent ids (overridable per deployment).
ARBITRATOR_AGENT_ID: str = os.environ.get("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
EXECUTOR_AGENT_ID: str = os.environ.get("DELPHI_EXECUTOR_AGENT_ID", "dev-codex")

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

_VALID_ROLES = {"worker", "arbitrator", "executor"}

# Validate seed agents fail-loud and build the HMAC secret lookup.
AGENT_SECRETS: dict[str, str] = {}
for _agent in SEED_AGENTS:
    _agent_id = _agent.get("agent_id")
    _host = _agent.get("host")
    _role = _agent.get("role")
    if not _agent_id or not _host or not _role:
        raise ValueError(
            f"Agent entry in {_agents_file} missing required field "
            "(agent_id/host/role): "
            f"{_agent!r}"
        )
    if _role not in _VALID_ROLES:
        raise ValueError(
            f"Agent '{_agent_id}' has invalid role {_role!r} in {_agents_file}. "
            f"Valid roles: {sorted(_VALID_ROLES)}"
        )
    _secret = _agent.get("secret", "")
    if not _secret or _secret.startswith("GENERATE_WITH"):
        raise ValueError(
            f"Agent '{_agent_id}' has no valid secret in {_agents_file}. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    AGENT_SECRETS[_agent_id] = _secret
