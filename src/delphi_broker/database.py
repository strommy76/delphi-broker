"""
--------------------------------------------------------------------------------
FILE:        database.py
PATH:        C:/Projects/delphi-broker/src/delphi_broker/database.py
DESCRIPTION: SQLite database layer — schema, HMAC verification, message
             lifecycle, and per-recipient delivery tracking.

CHANGELOG:
2026-03-31 17:30      Claude      [Harden] HMAC on all mutations, per-recipient
                                     receipts, replay protection, schema migration,
                                     agent verification on all paths
2026-03-31 16:30      Claude      [Harden] HMAC-SHA256 signing, exact recipient
                                     matching, atomic transitions, kill auto-reg
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .config import DB_PATH, SEED_AGENTS, WEB_UI_AGENT_ID, WEB_UI_ROLES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    roles       TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    metadata    TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL UNIQUE,
    channel       TEXT NOT NULL,
    sender        TEXT NOT NULL,
    recipients    TEXT NOT NULL DEFAULT '*',
    subject       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL,
    priority      TEXT NOT NULL DEFAULT 'normal',
    status        TEXT NOT NULL DEFAULT 'PENDING',
    submitted_at  TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT,
    decision_note TEXT DEFAULT '',
    parent_id     TEXT,
    metadata      TEXT DEFAULT '{}',
    signature     TEXT DEFAULT '',
    client_ts     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS message_receipts (
    message_id  TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    acked_at    TEXT,
    PRIMARY KEY (message_id, recipient)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_messages_submitted_at ON messages(submitted_at);
CREATE INDEX IF NOT EXISTS idx_messages_parent_id ON messages(parent_id);
"""

# Indexes that depend on migrated columns — applied after ALTER TABLE
_POST_MIGRATION_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_signature_unique
    ON messages(signature) WHERE signature != '';
"""

# Columns that may not exist in older databases — migrated on startup
_MIGRATIONS = [
    ("messages", "signature", "TEXT DEFAULT ''"),
    ("messages", "client_ts", "TEXT DEFAULT ''"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_initialized: set[str] = set()

# Replay window: reject signed messages older than this
_REPLAY_WINDOW = timedelta(minutes=5)


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    path_key = str(path)
    if path_key not in _initialized:
        init_db(conn)
        _initialized.add(path_key)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _apply_migrations(conn)
    conn.executescript(_POST_MIGRATION_SQL)
    now = _now()
    for agent in SEED_AGENTS:
        conn.execute(
            """INSERT OR IGNORE INTO agents (agent_id, host, roles, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (agent["agent_id"], agent["host"], agent["roles"], now, now),
        )
    conn.execute(
        """INSERT OR IGNORE INTO agents (agent_id, host, roles, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)""",
        (WEB_UI_AGENT_ID, "web", WEB_UI_ROLES, now, now),
    )
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add missing columns to existing tables (safe for fresh and upgraded DBs)."""
    for table, column, col_type in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------


def canonical_metadata_json(metadata: Optional[dict]) -> str:
    """Serialize metadata deterministically for signature and persistence."""
    return json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))


def _optional_text(value: Optional[str]) -> str:
    return value or ""


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def compute_signature(secret: str, *fields: str) -> str:
    """Compute HMAC-SHA256 over pipe-delimited fields.

    Every call site passes an action prefix as the first field to prevent
    cross-action signature reuse.
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


def build_submit_signature_fields(
    *,
    sender: str,
    channel: str,
    timestamp: str,
    subject: str,
    body: str,
    recipients: str,
    priority: str,
    parent_id: Optional[str],
    metadata: Optional[dict],
) -> tuple[str, ...]:
    return (
        "submit",
        sender,
        channel,
        timestamp,
        subject,
        body,
        recipients,
        priority,
        _optional_text(parent_id),
        canonical_metadata_json(metadata),
    )


def build_approve_signature_fields(
    *, agent_id: str, message_id: str, timestamp: str, note: str
) -> tuple[str, ...]:
    return ("approve", agent_id, message_id, timestamp, note)


def build_reject_signature_fields(
    *, agent_id: str, message_id: str, timestamp: str, reason: str
) -> tuple[str, ...]:
    return ("reject", agent_id, message_id, timestamp, reason)


def build_ack_signature_fields(
    *, agent_id: str, message_id: str, timestamp: str
) -> tuple[str, ...]:
    return ("ack", agent_id, message_id, timestamp)


def build_broadcast_signature_fields(
    *,
    sender: str,
    channel: str,
    timestamp: str,
    subject: str,
    body: str,
    priority: str,
    auto_approve: bool,
) -> tuple[str, ...]:
    return (
        "broadcast",
        sender,
        channel,
        timestamp,
        subject,
        body,
        priority,
        _bool_text(auto_approve),
    )


# ---------------------------------------------------------------------------
# Agent registry
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


def is_orchestrator(conn: sqlite3.Connection, agent_id: str) -> bool:
    cur = conn.execute("SELECT roles FROM agents WHERE agent_id = ?", (agent_id,))
    row = cur.fetchone()
    if not row:
        return False
    return "orchestrator" in row["roles"].split(",")


def message_targets_agent(recipients: str, agent_id: str) -> bool:
    if recipients == "*":
        return True
    return agent_id in [part.strip() for part in recipients.split(",") if part.strip()]


def can_agent_ack_message(message: dict, agent_id: str) -> bool:
    return bool(
        message
        and message.get("status") == "APPROVED"
        and message_targets_agent(message.get("recipients", ""), agent_id)
    )


# ---------------------------------------------------------------------------
# Message lifecycle
# ---------------------------------------------------------------------------


def submit_message(
    conn: sqlite3.Connection,
    *,
    sender: str,
    channel: str,
    subject: str,
    body: str,
    recipients: str = "*",
    priority: str = "normal",
    parent_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: str = "PENDING",
    signature: str = "",
    client_ts: str = "",
) -> dict:
    message_id = str(uuid.uuid4())
    now = _now()
    decided_at = now if status == "APPROVED" else None
    decided_by = sender if status == "APPROVED" else None
    conn.execute(
        """INSERT INTO messages
           (message_id, channel, sender, recipients, subject, body, priority,
            status, submitted_at, decided_at, decided_by, parent_id, metadata,
            signature, client_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            channel,
            sender,
            recipients,
            subject,
            body,
            priority,
            status,
            now,
            decided_at,
            decided_by,
            parent_id,
            canonical_metadata_json(metadata),
            signature,
            client_ts,
        ),
    )
    conn.commit()
    touch_agent(conn, sender)
    return {"message_id": message_id, "status": status, "submitted_at": now}


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,))
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def list_messages(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    channel: Optional[str] = None,
    recipient: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    exclude_acked: bool = False,
) -> list[dict]:
    clauses = []
    params: list = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if recipient:
        clauses.append(
            "(recipients = '*' OR recipients = ? "
            "OR recipients LIKE ? "
            "OR recipients LIKE ? "
            "OR recipients LIKE ?)"
        )
        params.extend(
            [
                recipient,  # exact single recipient
                f"{recipient},%",  # first in list
                f"%,{recipient},%",  # middle of list
                f"%,{recipient}",  # last in list
            ]
        )
    if since:
        clauses.append("submitted_at > ?")
        params.append(since)
    if exclude_acked and recipient:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM message_receipts r "
            "WHERE r.message_id = messages.message_id "
            "AND r.recipient = ? AND r.acked_at IS NOT NULL)"
        )
        params.append(recipient)

    where = " AND ".join(clauses) if clauses else "1=1"
    cur = conn.execute(
        f"SELECT * FROM messages WHERE {where} ORDER BY submitted_at DESC LIMIT ?",
        params + [limit],
    )
    return [_row_to_dict(row) for row in cur.fetchall()]


def approve_message(
    conn: sqlite3.Connection, message_id: str, agent_id: str, note: str = ""
) -> Optional[dict]:
    now = _now()
    cur = conn.execute(
        """UPDATE messages SET status = 'APPROVED', decided_at = ?,
           decided_by = ?, decision_note = ?
           WHERE message_id = ? AND status = 'PENDING'""",
        (now, agent_id, note, message_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    touch_agent(conn, agent_id)
    return get_message(conn, message_id)


def reject_message(
    conn: sqlite3.Connection, message_id: str, agent_id: str, reason: str = ""
) -> Optional[dict]:
    now = _now()
    cur = conn.execute(
        """UPDATE messages SET status = 'REJECTED', decided_at = ?,
           decided_by = ?, decision_note = ?
           WHERE message_id = ? AND status = 'PENDING'""",
        (now, agent_id, reason, message_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    touch_agent(conn, agent_id)
    return get_message(conn, message_id)


def ack_message(conn: sqlite3.Connection, message_id: str, agent_id: str) -> Optional[dict]:
    """Record per-recipient acknowledgement via message_receipts."""
    msg = get_message(conn, message_id)
    if not can_agent_ack_message(msg or {}, agent_id):
        return None
    now = _now()
    conn.execute(
        """INSERT INTO message_receipts (message_id, recipient, acked_at)
           VALUES (?, ?, ?)
           ON CONFLICT(message_id, recipient) DO NOTHING""",
        (message_id, agent_id, now),
    )
    conn.commit()
    touch_agent(conn, agent_id)
    receipt = conn.execute(
        "SELECT acked_at FROM message_receipts WHERE message_id = ? AND recipient = ?",
        (message_id, agent_id),
    ).fetchone()
    result = get_message(conn, message_id)
    if result and receipt:
        result["acked_by"] = agent_id
        result["acked_at"] = receipt["acked_at"]
    return result


def get_receipts(conn: sqlite3.Connection, message_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM message_receipts WHERE message_id = ? ORDER BY acked_at",
        (message_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def list_replies(conn: sqlite3.Connection, parent_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT * FROM messages WHERE parent_id = ? ORDER BY submitted_at ASC",
        (parent_id,),
    )
    return [_row_to_dict(row) for row in cur.fetchall()]


def list_agents(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")
    return [dict(row) for row in cur.fetchall()]


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("""SELECT channel,
                  COUNT(*) as total,
                  SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) as pending,
                  SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END) as approved,
                  SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) as rejected
           FROM messages GROUP BY channel ORDER BY MAX(submitted_at) DESC""")
    return [dict(row) for row in cur.fetchall()]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "metadata" in d and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
    d.pop("id", None)
    return d
