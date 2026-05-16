"""
--------------------------------------------------------------------------------
FILE:        database.py
PATH:        ~/projects/agent-broker/src/agent_broker/database.py
DESCRIPTION: SQLite data layer for Delphi v2 session, round, iteration, review, and agent state.

CHANGELOG:
2026-05-16 10:40      pi-claude  [Fix] Add PRAGMA busy_timeout=5000 to get_connection to prevent immediate lock-fail under poll-loop contention.
2026-05-06 14:21      Codex      [Fix] Upsert config-seeded agent registry rows so config remains canonical.
2026-05-06 14:04      Codex      [Fix] Rebuild the agents registry table with FK enforcement paused only for the migration window.
2026-05-06 14:00      Codex      [Fix] Make the agents registry shape migration restart-safe after failed attempts.
2026-05-06 13:52      Codex      [Fix] Persist probe identity markers so HMAC auth works without joining Delphi worker lanes.
2026-05-06 13:36      Codex      [Fix] Keep probe and operator participants out of the legacy Delphi agents table.
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------

SQLite data layer for the v2 iterative-pipeline broker.

Schema, DAO operations, and HMAC signature builders for the session / round /
iteration / review model defined in `DESIGN.md`. The v1 messages and
message_receipts tables and their lifecycle are gone; nothing in this module
references them.

All public functions are synchronous (stdlib `sqlite3`). Callers commit per
operation so the broker never leaves a half-applied transition on disk. Fail
loud on any contract violation: missing FK, invalid enum value, unknown
agent_id, etc.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import DB_PATH, SEED_AGENTS

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    problem_text       TEXT NOT NULL,
    status             TEXT NOT NULL CHECK(status IN (
                            'drafting','round_1','round_2','round_3',
                            'executing','complete','aborted','escalated')),
    nudge_window_secs  INTEGER NOT NULL DEFAULT 60,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    finalized_prompt   TEXT,
    skipped_reviewers  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS rounds (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    round_num     INTEGER NOT NULL,
    round_type    TEXT NOT NULL CHECK(round_type IN (
                       'same_host_pair','cross_host_arbitration',
                       'multi_agent_review','execute')),
    host          TEXT,
    status        TEXT NOT NULL CHECK(status IN (
                       'pending','in_progress','converged','escalated',
                       'complete','aborted')),
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    outcome_text  TEXT
);

CREATE TABLE IF NOT EXISTS iterations (
    id                       TEXT PRIMARY KEY,
    round_id                 TEXT NOT NULL REFERENCES rounds(id),
    iter_num                 INTEGER NOT NULL,
    source_agent             TEXT,
    destination_agent        TEXT NOT NULL,
    source_output            TEXT NOT NULL,
    nudge_text               TEXT,
    nudge_window_closes_at   TEXT NOT NULL,
    destination_output       TEXT,
    destination_self_assess  TEXT CHECK(destination_self_assess IN (
                                  'converged','more_work_needed')),
    destination_rationale    TEXT,
    source_emitted_at        TEXT NOT NULL,
    destination_received_at  TEXT,
    destination_emitted_at   TEXT,
    status                   TEXT NOT NULL CHECK(status IN (
                                  'awaiting_nudge','awaiting_destination',
                                  'complete','off_script'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id              TEXT PRIMARY KEY,
    round_id        TEXT NOT NULL REFERENCES rounds(id),
    reviewer_agent  TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    comments        TEXT,
    rationale       TEXT,
    emitted_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('worker','arbitrator','executor','operator')),
    is_probe    INTEGER NOT NULL DEFAULT 0 CHECK(is_probe IN (0,1)),
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_status         ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_rounds_session_round    ON rounds(session_id, round_num);
CREATE INDEX IF NOT EXISTS idx_iterations_round_iter   ON iterations(round_id, iter_num);
CREATE INDEX IF NOT EXISTS idx_iterations_status       ON iterations(status);
CREATE INDEX IF NOT EXISTS idx_reviews_round           ON reviews(round_id);
"""

# ---------------------------------------------------------------------------
# Enum tuples (mirror Pydantic enums; keep here to avoid circular imports)
# ---------------------------------------------------------------------------

_SESSION_STATUSES = (
    "drafting",
    "round_1",
    "round_2",
    "round_3",
    "executing",
    "complete",
    "aborted",
    "escalated",
)
_ROUND_TYPES = (
    "same_host_pair",
    "cross_host_arbitration",
    "multi_agent_review",
    "execute",
)
_ROUND_STATUSES = (
    "pending",
    "in_progress",
    "converged",
    "escalated",
    "complete",
    "aborted",
)
_ITERATION_STATUSES = (
    "awaiting_nudge",
    "awaiting_destination",
    "complete",
    "off_script",
)
_SELF_ASSESSMENTS = ("converged", "more_work_needed")
_REVIEW_DECISIONS = ("approve", "reject")

# Replay window: reject signed payloads with timestamps older than this.
_REPLAY_WINDOW = timedelta(minutes=5)

_initialized: set[str] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """Return the current instant as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)


def _require(value: object, label: str) -> None:
    if value is None or (isinstance(value, str) and not value):
        raise ValueError(f"{label} is required")


def _check_enum(value: str, valid: tuple[str, ...], label: str) -> None:
    if value not in valid:
        raise ValueError(f"{label} {value!r} not in {valid}")


def _optional_text(value: Optional[str]) -> str:
    return value or ""


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _bool_int(value: bool) -> int:
    return 1 if value else 0


# ---------------------------------------------------------------------------
# Connection / init
# ---------------------------------------------------------------------------


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection, initializing the schema on first use."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    path_key = str(path)
    if path_key not in _initialized:
        init_db(conn)
        _initialized.add(path_key)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the v2 schema (idempotent) and seed the agents table."""
    conn.executescript(_SCHEMA)
    _apply_migrations(conn)
    now = _now()
    for agent in SEED_AGENTS:
        if agent["participant_type"] not in {"agent", "operator"}:
            continue
        conn.execute(
            """INSERT INTO agents
                  (agent_id, host, role, is_probe, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                   host = excluded.host,
                   role = excluded.role,
                   is_probe = excluded.is_probe,
                   last_seen = excluded.last_seen""",
            (
                agent["agent_id"],
                agent["host"],
                agent["role"],
                _bool_int(agent["is_probe"]),
                now,
                now,
            ),
        )
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring older test DBs up to current schema. Idempotent."""
    cur = conn.execute("PRAGMA table_info(sessions)")
    cols = {row["name"] for row in cur.fetchall()}
    if "skipped_reviewers" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN skipped_reviewers TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
    _migrate_agents_registry_shape(conn)


def _migrate_agents_registry_shape(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(agents)")
    cols = {row["name"] for row in cur.fetchall()}
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()["sql"]
    if "is_probe" in cols and "'operator'" in sql:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("""
            DROP TABLE IF EXISTS agents_new;
            CREATE TABLE agents_new (
                agent_id    TEXT PRIMARY KEY,
                host        TEXT NOT NULL,
                role        TEXT NOT NULL CHECK(role IN ('worker','arbitrator','executor','operator')),
                is_probe    INTEGER NOT NULL DEFAULT 0 CHECK(is_probe IN (0,1)),
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL
            );
            INSERT INTO agents_new (agent_id, host, role, is_probe, first_seen, last_seen)
            SELECT agent_id, host, role, 0, first_seen, last_seen
              FROM agents
             WHERE role IN ('worker','arbitrator','executor','operator');
            DROP TABLE agents;
            ALTER TABLE agents_new RENAME TO agents;
            """)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    *,
    problem_text: str,
    nudge_window_secs: int = 60,
) -> dict:
    _require(problem_text, "problem_text")
    if nudge_window_secs < 0:
        raise ValueError("nudge_window_secs must be >= 0")
    session_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO sessions
              (id, problem_text, status, nudge_window_secs, created_at, updated_at)
           VALUES (?, ?, 'drafting', ?, ?, ?)""",
        (session_id, problem_text, nudge_window_secs, now, now),
    )
    conn.commit()
    session = get_session(conn, session_id)
    if session is None:  # pragma: no cover - defensive
        raise RuntimeError("session insert succeeded but row not found")
    return session


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return _row_to_dict(cur.fetchone())


def list_sessions(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    if status is not None:
        _check_enum(status, _SESSION_STATUSES, "session status")
        cur = conn.execute(
            "SELECT * FROM sessions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(row) for row in cur.fetchall()]


def update_session_status(conn: sqlite3.Connection, session_id: str, status: str) -> dict:
    _check_enum(status, _SESSION_STATUSES, "session status")
    now = _now()
    cur = conn.execute(
        "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, session_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"unknown session_id {session_id!r}")
    conn.commit()
    session = get_session(conn, session_id)
    if session is None:  # pragma: no cover - defensive
        raise RuntimeError("session disappeared during status update")
    return session


def get_skipped_reviewers(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """Return the JSON-decoded list of skipped reviewer agent ids on a session."""
    session = get_session(conn, session_id)
    if session is None:
        raise ValueError(f"unknown session_id {session_id!r}")
    raw = session.get("skipped_reviewers") or "[]"
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"session {session_id!r} has corrupt skipped_reviewers: {raw!r}") from exc
    if not isinstance(loaded, list) or not all(isinstance(x, str) for x in loaded):
        raise ValueError(
            f"session {session_id!r} skipped_reviewers must be JSON list[str], got {loaded!r}"
        )
    return loaded


def add_skipped_reviewer(conn: sqlite3.Connection, session_id: str, agent_id: str) -> None:
    """Append an agent_id to the session's skipped_reviewers list (no duplicates)."""
    _require(agent_id, "agent_id")
    current = get_skipped_reviewers(conn, session_id)
    if agent_id in current:
        return
    current.append(agent_id)
    now = _now()
    cur = conn.execute(
        "UPDATE sessions SET skipped_reviewers = ?, updated_at = ? WHERE id = ?",
        (json.dumps(current), now, session_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"unknown session_id {session_id!r}")
    conn.commit()


def round_2_outcome_for_session(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    """Return the outcome_text from the latest cross_host_arbitration round."""
    cur = conn.execute(
        """SELECT outcome_text FROM rounds
            WHERE session_id = ?
              AND round_type = 'cross_host_arbitration'
         ORDER BY round_num DESC, started_at DESC
            LIMIT 1""",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row["outcome_text"]


def set_finalized_prompt(conn: sqlite3.Connection, session_id: str, prompt: str) -> None:
    _require(prompt, "prompt")
    now = _now()
    cur = conn.execute(
        "UPDATE sessions SET finalized_prompt = ?, updated_at = ? WHERE id = ?",
        (prompt, now, session_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"unknown session_id {session_id!r}")
    conn.commit()


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------


def create_round(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    round_num: int,
    round_type: str,
    host: Optional[str] = None,
) -> dict:
    if get_session(conn, session_id) is None:
        raise ValueError(f"unknown session_id {session_id!r}")
    if round_num < 1:
        raise ValueError("round_num must be >= 1")
    _check_enum(round_type, _ROUND_TYPES, "round_type")
    round_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO rounds
              (id, session_id, round_num, round_type, host, status, started_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (round_id, session_id, round_num, round_type, host, now),
    )
    conn.commit()
    rnd = get_round(conn, round_id)
    if rnd is None:  # pragma: no cover - defensive
        raise RuntimeError("round insert succeeded but row not found")
    return rnd


def get_round(conn: sqlite3.Connection, round_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,))
    return _row_to_dict(cur.fetchone())


def list_rounds_for_session(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    cur = conn.execute(
        """SELECT * FROM rounds WHERE session_id = ?
           ORDER BY round_num ASC, host ASC""",
        (session_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def update_round_status(
    conn: sqlite3.Connection,
    round_id: str,
    status: str,
    outcome_text: Optional[str] = None,
) -> dict:
    _check_enum(status, _ROUND_STATUSES, "round status")
    now = _now()
    terminal = {"converged", "complete", "aborted", "escalated"}
    if status in terminal:
        cur = conn.execute(
            """UPDATE rounds
                  SET status = ?, outcome_text = COALESCE(?, outcome_text), ended_at = ?
                WHERE id = ?""",
            (status, outcome_text, now, round_id),
        )
    else:
        cur = conn.execute(
            """UPDATE rounds
                  SET status = ?, outcome_text = COALESCE(?, outcome_text)
                WHERE id = ?""",
            (status, outcome_text, round_id),
        )
    if cur.rowcount == 0:
        raise ValueError(f"unknown round_id {round_id!r}")
    conn.commit()
    rnd = get_round(conn, round_id)
    if rnd is None:  # pragma: no cover - defensive
        raise RuntimeError("round disappeared during status update")
    return rnd


def current_round_for_session(conn: sqlite3.Connection, session_id: str) -> Optional[dict]:
    """Return the highest-numbered round that is still pending or in-progress."""
    cur = conn.execute(
        """SELECT * FROM rounds
            WHERE session_id = ? AND status IN ('pending','in_progress')
         ORDER BY round_num DESC, started_at DESC
            LIMIT 1""",
        (session_id,),
    )
    return _row_to_dict(cur.fetchone())


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------


def create_iteration(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    iter_num: int,
    source_agent: Optional[str],
    destination_agent: str,
    source_output: str,
    nudge_window_secs: int,
) -> dict:
    if get_round(conn, round_id) is None:
        raise ValueError(f"unknown round_id {round_id!r}")
    _require(destination_agent, "destination_agent")
    _require(source_output, "source_output")
    if iter_num < 1:
        raise ValueError("iter_num must be >= 1")
    if nudge_window_secs < 0:
        raise ValueError("nudge_window_secs must be >= 0")

    iter_id = _new_id()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    closes_at = (now_dt + timedelta(seconds=nudge_window_secs)).isoformat()

    conn.execute(
        """INSERT INTO iterations
              (id, round_id, iter_num, source_agent, destination_agent,
               source_output, nudge_text, nudge_window_closes_at,
               source_emitted_at, status)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'awaiting_nudge')""",
        (
            iter_id,
            round_id,
            iter_num,
            source_agent,
            destination_agent,
            source_output,
            closes_at,
            now,
        ),
    )
    conn.commit()
    iteration = get_iteration(conn, iter_id)
    if iteration is None:  # pragma: no cover - defensive
        raise RuntimeError("iteration insert succeeded but row not found")
    return iteration


def get_iteration(conn: sqlite3.Connection, iteration_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM iterations WHERE id = ?", (iteration_id,))
    return _row_to_dict(cur.fetchone())


def list_iterations_for_round(conn: sqlite3.Connection, round_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM iterations WHERE round_id = ? ORDER BY iter_num ASC",
        (round_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def latest_iteration_for_round(conn: sqlite3.Connection, round_id: str) -> Optional[dict]:
    cur = conn.execute(
        """SELECT * FROM iterations WHERE round_id = ?
           ORDER BY iter_num DESC LIMIT 1""",
        (round_id,),
    )
    return _row_to_dict(cur.fetchone())


def _set_iteration_to_awaiting_destination(
    conn: sqlite3.Connection, iteration_id: str, nudge_text: Optional[str]
) -> dict:
    iteration = get_iteration(conn, iteration_id)
    if iteration is None:
        raise ValueError(f"unknown iteration_id {iteration_id!r}")
    if iteration["status"] != "awaiting_nudge":
        raise ValueError(
            f"iteration {iteration_id!r} is in status {iteration['status']!r}, "
            "expected 'awaiting_nudge'"
        )
    now = _now()
    conn.execute(
        """UPDATE iterations
              SET nudge_text = ?, status = 'awaiting_destination',
                  destination_received_at = ?
            WHERE id = ?""",
        (nudge_text, now, iteration_id),
    )
    conn.commit()
    updated = get_iteration(conn, iteration_id)
    if updated is None:  # pragma: no cover - defensive
        raise RuntimeError("iteration disappeared during nudge transition")
    return updated


def apply_nudge(conn: sqlite3.Connection, iteration_id: str, nudge_text: str) -> dict:
    _require(nudge_text, "nudge_text")
    return _set_iteration_to_awaiting_destination(conn, iteration_id, nudge_text)


def skip_nudge(conn: sqlite3.Connection, iteration_id: str) -> dict:
    return _set_iteration_to_awaiting_destination(conn, iteration_id, None)


def record_destination_response(
    conn: sqlite3.Connection,
    iteration_id: str,
    *,
    output: str,
    self_assessment: str,
    rationale: str,
) -> dict:
    _require(output, "output")
    _check_enum(self_assessment, _SELF_ASSESSMENTS, "self_assessment")
    iteration = get_iteration(conn, iteration_id)
    if iteration is None:
        raise ValueError(f"unknown iteration_id {iteration_id!r}")
    if iteration["status"] != "awaiting_destination":
        raise ValueError(
            f"iteration {iteration_id!r} is in status {iteration['status']!r}, "
            "expected 'awaiting_destination'"
        )
    now = _now()
    conn.execute(
        """UPDATE iterations
              SET destination_output = ?, destination_self_assess = ?,
                  destination_rationale = ?, destination_emitted_at = ?,
                  status = 'complete'
            WHERE id = ?""",
        (output, self_assessment, rationale, now, iteration_id),
    )
    conn.commit()
    updated = get_iteration(conn, iteration_id)
    if updated is None:  # pragma: no cover - defensive
        raise RuntimeError("iteration disappeared during response record")
    return updated


def mark_iteration_off_script(conn: sqlite3.Connection, iteration_id: str, reason: str) -> dict:
    _require(reason, "reason")
    iteration = get_iteration(conn, iteration_id)
    if iteration is None:
        raise ValueError(f"unknown iteration_id {iteration_id!r}")
    if iteration["status"] in ("complete", "off_script"):
        raise ValueError(f"iteration {iteration_id!r} already terminal ({iteration['status']!r})")
    conn.execute(
        """UPDATE iterations
              SET status = 'off_script', destination_rationale = ?
            WHERE id = ?""",
        (reason, iteration_id),
    )
    conn.commit()
    updated = get_iteration(conn, iteration_id)
    if updated is None:  # pragma: no cover - defensive
        raise RuntimeError("iteration disappeared during off_script mark")
    return updated


def find_pending_iterations(
    conn: sqlite3.Connection, session_id: Optional[str] = None
) -> list[dict]:
    """Return iterations awaiting nudge whose window has already closed.

    The caller is expected to feed each result into `skip_nudge` (default
    action when the operator hasn't responded in time).
    """
    now = _now()
    if session_id is None:
        cur = conn.execute(
            """SELECT i.* FROM iterations i
               WHERE i.status = 'awaiting_nudge'
                 AND i.nudge_window_closes_at <= ?
            ORDER BY i.source_emitted_at ASC""",
            (now,),
        )
    else:
        cur = conn.execute(
            """SELECT i.* FROM iterations i
                 JOIN rounds r ON r.id = i.round_id
               WHERE r.session_id = ?
                 AND i.status = 'awaiting_nudge'
                 AND i.nudge_window_closes_at <= ?
            ORDER BY i.source_emitted_at ASC""",
            (session_id, now),
        )
    return [dict(row) for row in cur.fetchall()]


def find_inbox_for_agent(conn: sqlite3.Connection, agent_id: str) -> list[dict]:
    """Return work items addressed to `agent_id`.

    Currently this returns iterations whose status is `awaiting_destination`
    and whose `destination_agent` matches the supplied agent. Round-3 review
    requests live in a separate table — callers that need to merge them in
    should query `reviews` and `rounds` directly (or use the workflow engine
    layer once it lands in a follow-up commit).
    """
    cur = conn.execute(
        """SELECT * FROM iterations
            WHERE status = 'awaiting_destination'
              AND destination_agent = ?
         ORDER BY destination_received_at ASC""",
        (agent_id,),
    )
    return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def create_review(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    reviewer_agent: str,
    decision: str,
    comments: Optional[str] = None,
    rationale: Optional[str] = None,
) -> dict:
    if get_round(conn, round_id) is None:
        raise ValueError(f"unknown round_id {round_id!r}")
    _require(reviewer_agent, "reviewer_agent")
    _check_enum(decision, _REVIEW_DECISIONS, "decision")
    review_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO reviews
              (id, round_id, reviewer_agent, decision, comments, rationale, emitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (review_id, round_id, reviewer_agent, decision, comments, rationale, now),
    )
    conn.commit()
    cur = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,))
    row = _row_to_dict(cur.fetchone())
    if row is None:  # pragma: no cover - defensive
        raise RuntimeError("review insert succeeded but row not found")
    return row


def list_reviews_for_round(conn: sqlite3.Connection, round_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM reviews WHERE round_id = ? ORDER BY emitted_at ASC",
        (round_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def find_pending_reviewers_for_round(
    conn: sqlite3.Connection, round_id: str, expected_reviewers: list[str]
) -> list[str]:
    """Return reviewer agent ids in `expected_reviewers` that haven't reviewed yet."""
    cur = conn.execute(
        "SELECT reviewer_agent FROM reviews WHERE round_id = ?",
        (round_id,),
    )
    submitted = {row["reviewer_agent"] for row in cur.fetchall()}
    return [r for r in expected_reviewers if r not in submitted]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def verify_agent(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Check that agent_id exists in the registry. No auto-registration."""
    cur = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,))
    return cur.fetchone() is not None


def touch_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    """Update last_seen for a known agent."""
    now = _now()
    conn.execute("UPDATE agents SET last_seen = ? WHERE agent_id = ?", (now, agent_id))
    conn.commit()


def get_agent(conn: sqlite3.Connection, agent_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
    return _row_to_dict(cur.fetchone())


def list_agents(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")
    return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# HMAC signing primitives (preserved from v1)
# ---------------------------------------------------------------------------


def canonical_metadata_json(metadata: Optional[dict]) -> str:
    """Serialize metadata deterministically for signing and persistence."""
    return json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))


def compute_signature(secret: str, *fields: str) -> str:
    """Compute HMAC-SHA256 over pipe-delimited fields.

    Every signature builder prefixes the action name as the first field so
    cross-action signature reuse is impossible.
    """
    canonical = "|".join(fields)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def check_timestamp_freshness(client_ts: str) -> bool:
    """Reject timestamps outside the replay window."""
    try:
        ts = datetime.fromisoformat(client_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    now = datetime.now(timezone.utc)
    return abs(now - ts) <= _REPLAY_WINDOW


# ---------------------------------------------------------------------------
# v2 signature builders
# ---------------------------------------------------------------------------


def build_create_session_signature_fields(
    *, sender: str, timestamp: str, problem_text: str
) -> tuple[str, ...]:
    return ("create_session", sender, timestamp, problem_text)


def build_nudge_signature_fields(
    *,
    sender: str,
    iteration_id: str,
    timestamp: str,
    action: str,
    nudge_text: Optional[str],
) -> tuple[str, ...]:
    return ("nudge", sender, iteration_id, timestamp, action, _optional_text(nudge_text))


def build_emit_response_signature_fields(
    *,
    agent_id: str,
    iteration_id: str,
    timestamp: str,
    output: str,
    self_assessment: str,
    rationale: Optional[str],
) -> tuple[str, ...]:
    return (
        "emit_response",
        agent_id,
        iteration_id,
        timestamp,
        output,
        self_assessment,
        _optional_text(rationale),
    )


def build_emit_review_signature_fields(
    *,
    agent_id: str,
    round_id: str,
    timestamp: str,
    decision: str,
    comments: Optional[str],
    rationale: Optional[str],
) -> tuple[str, ...]:
    return (
        "emit_review",
        agent_id,
        round_id,
        timestamp,
        decision,
        _optional_text(comments),
        _optional_text(rationale),
    )


def build_executor_emit_signature_fields(
    *,
    agent_id: str,
    iteration_id: str,
    timestamp: str,
    success: bool,
    output: str,
    error: Optional[str],
) -> tuple[str, ...]:
    return (
        "executor_emit",
        agent_id,
        iteration_id,
        timestamp,
        _bool_text(success),
        output,
        _optional_text(error),
    )


def build_collab_propose_signature_fields(
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    timestamp: str,
    correlation_id: str,
    to_participants: list[str] | tuple[str, ...],
    message_kind: str,
    payload_json: dict,
    content_text: str,
    thread_id: str | None,
    subject: str | None,
) -> tuple[str, ...]:
    return (
        "collab_propose_message",
        agent_id,
        participant_type,
        transport_type,
        timestamp,
        correlation_id,
        json.dumps(list(to_participants), sort_keys=True, separators=(",", ":")),
        message_kind,
        json.dumps(payload_json, sort_keys=True, separators=(",", ":")),
        content_text,
        "" if thread_id is None else thread_id,
        "" if subject is None else subject,
    )


def build_collab_poll_signature_fields(
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    timestamp: str,
    limit: int,
) -> tuple[str, ...]:
    return (
        "collab_poll",
        agent_id,
        participant_type,
        transport_type,
        timestamp,
        str(limit),
    )


def build_collab_ack_signature_fields(
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    timestamp: str,
    deliverable_id: str,
) -> tuple[str, ...]:
    return (
        "collab_ack",
        agent_id,
        participant_type,
        transport_type,
        timestamp,
        deliverable_id,
    )


def build_collab_get_thread_signature_fields(
    *,
    agent_id: str,
    participant_type: str,
    transport_type: str,
    timestamp: str,
    thread_id: str,
) -> tuple[str, ...]:
    return (
        "collab_get_thread",
        agent_id,
        participant_type,
        transport_type,
        timestamp,
        thread_id,
    )


def build_abort_signature_fields(
    *, sender: str, session_id: str, timestamp: str
) -> tuple[str, ...]:
    return ("abort", sender, session_id, timestamp)
