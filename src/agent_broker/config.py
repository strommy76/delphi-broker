"""
--------------------------------------------------------------------------------
FILE:        config.py
PATH:        ~/projects/agent-broker/src/agent_broker/config.py
DESCRIPTION: Fail-loud configuration loader for broker runtime, MCP transport, and agent registry settings.

CHANGELOG:
2026-05-16 12:20      Claude     [Refactor] Move per-agent HMAC secrets from inline/sidecar JSON to .env per the env-SSOT doctrine. agents.json carries structural fields only; secrets come from DELPHI_AGENT_SECRET_<NORMALIZED_AGENT_ID>. Inline `secret` retained as back-compat for test fixtures.
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
are sourced from `.env` via convention `DELPHI_AGENT_SECRET_<NORMALIZED_AGENT_ID>`
(uppercase, `-` replaced with `_`). The agent registry carries structural
fields only and is committed; secrets stay in `.env` (gitignored). Inline
`secret` on an agent record is retained as back-compat for test fixtures.

Fail-loud on any missing or invalid configuration. Silent fallbacks are
forbidden by project policy.
"""

from __future__ import annotations

import json
import os
import ipaddress
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


def _require_present_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"{name} is required")
    return value.strip()


def _require_cidr_csv_env(
    name: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw_value = _require_present_env(name)
    values = tuple(part.strip() for part in raw_value.split(",") if part.strip())
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=True))
        except ValueError as exc:
            raise RuntimeError(f"{name} contains invalid CIDR {value!r}") from exc
    return tuple(networks)


HOST: str = _require_env("DELPHI_HOST")
PORT: int = _require_int_env("DELPHI_PORT")
MCP_HOST_REGISTRY: tuple[str, ...] = _require_csv_env("DELPHI_MCP_HOST_REGISTRY")
MCP_ORIGIN_REGISTRY: tuple[str, ...] = _require_csv_env("DELPHI_MCP_ORIGIN_REGISTRY")
ORIGINLESS_TRUSTED_INGRESS_CIDRS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    _require_cidr_csv_env("DELPHI_ORIGINLESS_TRUSTED_INGRESS_CIDRS")
)

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

# Validate seed agents fail-loud and build the HMAC secret lookup. Secrets
# come from `.env` (DELPHI_AGENT_SECRET_<NORMALIZED_AGENT_ID>) per the
# env-SSOT doctrine. An inline `secret` field on an agent record is honored
# as back-compat for test fixtures, but production agents.json carries
# structural fields only.
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
    # Inline `secret` field is back-compat for test fixtures only. Production
    # agents.json must not carry secrets; .env is the SSOT per the env-SSOT
    # doctrine. Inline values that look like the GENERATE_WITH placeholder are
    # treated as not-yet-generated.
    _inline_secret = (_agent.get("secret") or "").strip()
    if _inline_secret and not _inline_secret.startswith("GENERATE_WITH"):
        AGENT_SECRETS[_agent_id] = _inline_secret

# Per-agent HMAC secrets sourced from .env per the env-SSOT doctrine.
# Convention: DELPHI_AGENT_SECRET_<agent_id_normalized>, where normalization
# uppercases the agent_id and replaces `-` with `_`. An env-var value
# overrides an inline back-compat value if both are present.
def _agent_secret_env_var(agent_id: str) -> str:
    return "DELPHI_AGENT_SECRET_" + agent_id.upper().replace("-", "_")


for _agent in SEED_AGENTS:
    _agent_id = _agent["agent_id"]
    _env_secret = os.environ.get(_agent_secret_env_var(_agent_id), "").strip()
    if _env_secret:
        AGENT_SECRETS[_agent_id] = _env_secret

# Final validation: every seed agent must have a usable secret somewhere.
for _agent in SEED_AGENTS:
    _agent_id = _agent["agent_id"]
    if _agent_id not in AGENT_SECRETS:
        raise ValueError(
            f"Agent '{_agent_id}' has no valid secret. Set environment variable "
            f"{_agent_secret_env_var(_agent_id)} in .env (preferred), or for "
            f"test fixtures inline `secret` in the agents.json entry. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )

if OPERATOR_PARTICIPANT_ID not in {agent["agent_id"] for agent in SEED_AGENTS}:
    raise ValueError(
        f"OPERATOR_PARTICIPANT_ID {OPERATOR_PARTICIPANT_ID!r} is not registered in {_agents_file}"
    )
