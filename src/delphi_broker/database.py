"""SQLite database layer for Delphi Broker."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
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
    acked_at      TEXT,
    acked_by      TEXT,
    parent_id     TEXT,
    metadata      TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_messages_submitted_at ON messages(submitted_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_initialized: set[str] = set()


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


def touch_agent(conn: sqlite3.Connection, agent_id: str, host: str = "") -> None:
    now = _now()
    cur = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,))
    if cur.fetchone():
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE agent_id = ?", (now, agent_id)
        )
    else:
        conn.execute(
            """INSERT INTO agents (agent_id, host, roles, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (agent_id, host or "unknown", "worker", now, now),
        )
    conn.commit()


def is_orchestrator(conn: sqlite3.Connection, agent_id: str) -> bool:
    cur = conn.execute(
        "SELECT roles FROM agents WHERE agent_id = ?", (agent_id,)
    )
    row = cur.fetchone()
    if not row:
        return False
    return "orchestrator" in row["roles"].split(",")


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
) -> dict:
    message_id = str(uuid.uuid4())
    now = _now()
    decided_at = now if status == "APPROVED" else None
    decided_by = sender if status == "APPROVED" else None
    conn.execute(
        """INSERT INTO messages
           (message_id, channel, sender, recipients, subject, body, priority,
            status, submitted_at, decided_at, decided_by, parent_id, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id, channel, sender, recipients, subject, body, priority,
            status, now, decided_at, decided_by, parent_id,
            json.dumps(metadata or {}),
        ),
    )
    conn.commit()
    touch_agent(conn, sender)
    return {"message_id": message_id, "status": status, "submitted_at": now}


def get_message(conn: sqlite3.Connection, message_id: str) -> Optional[dict]:
    cur = conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", (message_id,)
    )
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
        clauses.append("(recipients = '*' OR recipients LIKE ?)")
        params.append(f"%{recipient}%")
    if since:
        clauses.append("submitted_at > ?")
        params.append(since)

    where = " AND ".join(clauses) if clauses else "1=1"
    cur = conn.execute(
        f"SELECT * FROM messages WHERE {where} ORDER BY submitted_at DESC LIMIT ?",
        params + [limit],
    )
    return [_row_to_dict(row) for row in cur.fetchall()]


def approve_message(
    conn: sqlite3.Connection, message_id: str, agent_id: str, note: str = ""
) -> Optional[dict]:
    msg = get_message(conn, message_id)
    if not msg or msg["status"] != "PENDING":
        return None
    now = _now()
    conn.execute(
        """UPDATE messages SET status = 'APPROVED', decided_at = ?,
           decided_by = ?, decision_note = ? WHERE message_id = ?""",
        (now, agent_id, note, message_id),
    )
    conn.commit()
    touch_agent(conn, agent_id)
    return get_message(conn, message_id)


def reject_message(
    conn: sqlite3.Connection, message_id: str, agent_id: str, reason: str = ""
) -> Optional[dict]:
    msg = get_message(conn, message_id)
    if not msg or msg["status"] != "PENDING":
        return None
    now = _now()
    conn.execute(
        """UPDATE messages SET status = 'REJECTED', decided_at = ?,
           decided_by = ?, decision_note = ? WHERE message_id = ?""",
        (now, agent_id, reason, message_id),
    )
    conn.commit()
    touch_agent(conn, agent_id)
    return get_message(conn, message_id)


def ack_message(
    conn: sqlite3.Connection, message_id: str, agent_id: str
) -> Optional[dict]:
    msg = get_message(conn, message_id)
    if not msg or msg["status"] != "APPROVED":
        return None
    now = _now()
    conn.execute(
        """UPDATE messages SET status = 'ACKED', acked_at = ?, acked_by = ?
           WHERE message_id = ?""",
        (now, agent_id, message_id),
    )
    conn.commit()
    touch_agent(conn, agent_id)
    return get_message(conn, message_id)


def list_agents(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")
    return [dict(row) for row in cur.fetchall()]


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT channel,
                  COUNT(*) as total,
                  SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) as pending,
                  SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END) as approved,
                  SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) as rejected,
                  SUM(CASE WHEN status='ACKED' THEN 1 ELSE 0 END) as acked
           FROM messages GROUP BY channel ORDER BY MAX(submitted_at) DESC"""
    )
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
