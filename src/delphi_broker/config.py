"""Single import point for all configuration.

Infrastructure values come from the environment (or `.env` if present); the
agent registry is loaded from `config/agents.json`. Per-agent HMAC secrets
may also be split out into `config/agents-secrets.json` (gitignored) so the
public agent manifest can be checked into VCS without leaking secrets.

Fail-loud on any missing or invalid configuration. Silent fallbacks are
forbidden by project policy.
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
# session-creator. Both names supported: DELPHI_OPERATOR_TOKEN is the v2
# canonical name; DELPHI_WEB_UI_PASSWORD is the v1 fallback.
OPERATOR_TOKEN: str = (
    os.environ.get("DELPHI_OPERATOR_TOKEN", "").strip()
    or os.environ.get("DELPHI_WEB_UI_PASSWORD", "").strip()
)
WEB_UI_PASSWORD: str = OPERATOR_TOKEN  # back-compat alias

# Cookie security toggle: set DELPHI_WEB_SECURE=1 when broker is fronted by HTTPS.
WEB_SECURE: bool = os.environ.get("DELPHI_WEB_SECURE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Designated arbitrator and executor agent ids (overridable per deployment).
ARBITRATOR_AGENT_ID: str = os.environ.get("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
EXECUTOR_AGENT_ID: str = os.environ.get("DELPHI_EXECUTOR_AGENT_ID", "dev-codex")


def require_operator_token() -> str:
    """Return the operator token, raising if it isn't configured.

    Called by the API/web layers at request time so importing config does not
    raise during tests that exercise unrelated subsystems.
    """
    if not OPERATOR_TOKEN:
        raise RuntimeError(
            "DELPHI_OPERATOR_TOKEN is not set. Generate one with "
            '`python -c "import secrets; print(secrets.token_hex(32))"`'
            " and put it in .env or the process environment."
        )
    return OPERATOR_TOKEN


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

# Validate seed agents fail-loud and build the HMAC secret lookup. Secrets may
# come from the agents.json entry directly (tests + back-compat) or from a
# sidecar `config/agents-secrets.json` file (preferred for production).
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
    _secret = (_agent.get("secret") or "").strip()
    if _secret and not _secret.startswith("GENERATE_WITH"):
        AGENT_SECRETS[_agent_id] = _secret

# Optional sidecar: config/agents-secrets.json (gitignored) overrides inline.
_secrets_path_raw = os.environ.get(
    "DELPHI_AGENT_SECRETS_PATH", "config/agents-secrets.json"
)
_secrets_file = (
    Path(_secrets_path_raw)
    if Path(_secrets_path_raw).is_absolute()
    else _PROJECT_ROOT / _secrets_path_raw
)
if _secrets_file.exists():
    _secrets_data = json.loads(_secrets_file.read_text())
    if not isinstance(_secrets_data, dict):
        raise ValueError(
            f"{_secrets_file}: top-level must be a JSON object "
            "of {agent_id: hex_secret}"
        )
    for _agent_id, _secret_value in _secrets_data.items():
        if not isinstance(_secret_value, str) or not _secret_value.strip():
            raise ValueError(
                f"{_secrets_file}: agent '{_agent_id}' has empty/invalid secret"
            )
        AGENT_SECRETS[_agent_id] = _secret_value.strip()

# Final validation: every seed agent must have a usable secret somewhere.
for _agent in SEED_AGENTS:
    _agent_id = _agent["agent_id"]
    if _agent_id not in AGENT_SECRETS:
        raise ValueError(
            f"Agent '{_agent_id}' has no valid secret. Either inline `secret` in "
            f"{_agents_file} or add an entry in {_secrets_file}. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
