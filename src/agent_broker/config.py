"""
--------------------------------------------------------------------------------
FILE:        config.py
PATH:        ~/projects/agent-broker/src/agent_broker/config.py
DESCRIPTION: Fail-loud configuration loader for broker runtime, MCP transport, and agent registry settings.

CHANGELOG:
2026-05-06 14:00      Codex      [Refactor] Rename operator permanently hidden thread path env-var for semantic consistency.
2026-05-06 13:37      Codex      [Refactor] Rename operator hidden-thread constant for permanent read-side exclusion semantics.
2026-05-06 12:52      Codex      [Feature] Add operator participant authority and operator transcript exclusion config.
2026-05-06 09:47      Codex      [Refactor] Fail loud when agent registry entries omit peer participant identity fields.
2026-05-06 08:40      Codex      [Refactor] Document fail-loud reads for nudge sweep and MCP session manager gates.
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------

Single import point for all configuration.

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
_TRUE_VALUES = {"1", "true", "yes"}

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


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value.strip()


def _require_int_env(name: str) -> int:
    value = _require_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _require_bool_env(name: str) -> bool:
    value = _require_env(name).lower()
    if value in _TRUE_VALUES:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{name} must be one of: 1,true,yes,0,false,no")


def _require_csv_env(name: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in _require_env(name).split(",") if part.strip())
    if not values:
        raise RuntimeError(f"{name} must contain at least one value")
    return values


HOST: str = _require_env("DELPHI_HOST")
PORT: int = _require_int_env("DELPHI_PORT")
MCP_HOST_REGISTRY: tuple[str, ...] = _require_csv_env("DELPHI_MCP_HOST_REGISTRY")
MCP_ORIGIN_REGISTRY: tuple[str, ...] = _require_csv_env("DELPHI_MCP_ORIGIN_REGISTRY")

_db_path_raw = _require_env("DELPHI_DB_PATH")
DB_PATH: Path = (
    Path(_db_path_raw) if Path(_db_path_raw).is_absolute() else _PROJECT_ROOT / _db_path_raw
)

# Operator (web UI) authentication. Single secret in .env binds the
# session-creator. DELPHI_OPERATOR_TOKEN is the only authority key.
OPERATOR_TOKEN: str | None = os.environ.get("DELPHI_OPERATOR_TOKEN")
if OPERATOR_TOKEN is not None:
    OPERATOR_TOKEN = OPERATOR_TOKEN.strip()
OPERATOR_PARTICIPANT_ID: str = _require_env("OPERATOR_PARTICIPANT_ID")

# Cookie security toggle: set DELPHI_WEB_SECURE=1 when broker is fronted by HTTPS.
WEB_SECURE: bool = _require_bool_env("DELPHI_WEB_SECURE")
NUDGE_SWEEP_ENABLED: bool = _require_bool_env("DELPHI_NUDGE_SWEEP_ENABLED")
MCP_SESSION_MANAGER_ENABLED: bool = _require_bool_env("DELPHI_MCP_SESSION_MANAGER_ENABLED")

# Designated arbitrator and executor agent ids (overridable per deployment).
ARBITRATOR_AGENT_ID: str = _require_env("DELPHI_ARBITRATOR_AGENT_ID")
EXECUTOR_AGENT_ID: str = _require_env("DELPHI_EXECUTOR_AGENT_ID")

_operator_permanently_hidden_threads_path_raw = _require_env(
    "OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH"
)
OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH: Path = (
    Path(_operator_permanently_hidden_threads_path_raw)
    if Path(_operator_permanently_hidden_threads_path_raw).is_absolute()
    else _PROJECT_ROOT / _operator_permanently_hidden_threads_path_raw
)


def _load_operator_permanently_hidden_thread_ids(path: Path) -> frozenset[str]:
    if not path.exists():
        raise FileNotFoundError(f"Operator permanently hidden thread config not found: {path}")
    data = json.loads(path.read_text())
    thread_ids = data.get("thread_ids")
    if not isinstance(thread_ids, list):
        raise ValueError(f"{path}: thread_ids must be a JSON array")
    invalid = [item for item in thread_ids if not isinstance(item, str) or not item.strip()]
    if invalid:
        raise ValueError(f"{path}: thread_ids contains invalid entries: {invalid!r}")
    return frozenset(item.strip() for item in thread_ids)


OPERATOR_PERMANENTLY_HIDDEN_THREAD_IDS: frozenset[str] = (
    _load_operator_permanently_hidden_thread_ids(OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH)
)


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
_agents_path_raw = _require_env("DELPHI_AGENTS_PATH")
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

_VALID_ROLES = {"worker", "arbitrator", "executor", "operator"}

# Validate seed agents fail-loud and build the HMAC secret lookup. Secrets may
# come from the agents.json entry directly (tests + back-compat) or from a
# sidecar `config/agents-secrets.json` file (preferred for production).
AGENT_SECRETS: dict[str, str] = {}
for _agent in SEED_AGENTS:
    _agent_id = _agent.get("agent_id")
    _host = _agent.get("host")
    _role = _agent.get("role")
    _participant_type = _agent.get("participant_type")
    _transport_type = _agent.get("transport_type")
    _is_probe = _agent.get("is_probe")
    _collaboration_governed = _agent.get("collaboration_governed")
    _missing = [
        _field
        for _field, _value in (
            ("agent_id", _agent_id),
            ("host", _host),
            ("role", _role),
            ("participant_type", _participant_type),
            ("transport_type", _transport_type),
            ("is_probe", _is_probe),
            ("collaboration_governed", _collaboration_governed),
        )
        if _value is None or _value == ""
    ]
    if _missing:
        raise ValueError(
            f"Agent entry in {_agents_file} missing required field {'/'.join(_missing)}: {_agent!r}"
        )
    if _role not in _VALID_ROLES:
        raise ValueError(
            f"Agent '{_agent_id}' has invalid role {_role!r} in {_agents_file}. "
            f"Valid roles: {sorted(_VALID_ROLES)}"
        )
    if not isinstance(_is_probe, bool):
        raise ValueError(
            f"Agent '{_agent_id}' has invalid is_probe {_is_probe!r} in {_agents_file}; "
            "expected true or false"
        )
    if not isinstance(_collaboration_governed, bool):
        raise ValueError(
            f"Agent '{_agent_id}' has invalid collaboration_governed "
            f"{_collaboration_governed!r} in {_agents_file}; expected true or false"
        )
    _secret = (_agent.get("secret") or "").strip()
    if _secret and not _secret.startswith("GENERATE_WITH"):
        AGENT_SECRETS[_agent_id] = _secret

# Optional sidecar: config/agents-secrets.json (gitignored) overrides inline.
_secrets_path_raw = _require_env("DELPHI_AGENT_SECRETS_PATH")
_secrets_file = (
    Path(_secrets_path_raw)
    if Path(_secrets_path_raw).is_absolute()
    else _PROJECT_ROOT / _secrets_path_raw
)
if _secrets_file.exists():
    _secrets_data = json.loads(_secrets_file.read_text())
    if not isinstance(_secrets_data, dict):
        raise ValueError(
            f"{_secrets_file}: top-level must be a JSON object of {{agent_id: hex_secret}}"
        )
    for _agent_id, _secret_value in _secrets_data.items():
        if not isinstance(_secret_value, str) or not _secret_value.strip():
            raise ValueError(f"{_secrets_file}: agent '{_agent_id}' has empty/invalid secret")
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

if OPERATOR_PARTICIPANT_ID not in {agent["agent_id"] for agent in SEED_AGENTS}:
    raise ValueError(
        f"OPERATOR_PARTICIPANT_ID {OPERATOR_PARTICIPANT_ID!r} is not registered in {_agents_file}"
    )
