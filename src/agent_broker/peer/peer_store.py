"""
--------------------------------------------------------------------------------
FILE:        peer_store.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/peer_store.py
DESCRIPTION: SQLite schema migration and persistence API for immutable peer messages, receipts, and audit events.

CHANGELOG:
2026-05-06 13:29      Codex      [Fix] Retire startup trigger drops and make peer_store the canonical audit-event seam.
2026-05-06 13:13      Codex      [Refactor] Keep poll receipt updates atomic without per-message receipt SELECTs.
2026-05-06 12:58      Codex      [Refactor] Remove probe cleanup mutation helper and add batched event/thread reads for operator segregation.
2026-05-06 11:49      Codex      [Fix] Close implicit read transactions before explicit atomic peer write windows.
2026-05-06 11:25      Codex      [Feature] Add operator transcript read helpers and audit-only mark-read persistence.
2026-05-06 11:13      Codex      [Fix] Add atomic poll/ack persistence helpers and batched receipt lookup for Phase 7 audit correctness.
2026-05-06 09:49      Codex      [Cleanup] Add narrow cleanup helper for reviewer FK probe artifact removal.
2026-05-06 09:47      Codex      [Refactor] Remove dead recipient/message columns, add recipient order, and add atomic send persistence.
2026-05-06 09:35      Codex      [Feature] Add receipt lookup helpers for Phase 5 peer delivery services.
2026-05-06 09:29      Codex      [Feature] Add Phase 4 peer messaging schema migration and DB-only store API.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_threads (
    thread_id  TEXT PRIMARY KEY,
    created_ts TEXT NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK(status IN ('open', 'closed', 'archived'))
);

CREATE TABLE IF NOT EXISTS peer_messages (
    message_id             TEXT PRIMARY KEY,
    thread_id              TEXT NOT NULL,
    from_participant       TEXT NOT NULL,
    from_participant_type  TEXT NOT NULL,
    from_transport_type    TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    content_text           TEXT NOT NULL,
    correlation_id         TEXT NOT NULL,
    parent_message_id      TEXT,
    sent_ts                TEXT NOT NULL,
    FOREIGN KEY(thread_id) REFERENCES peer_threads(thread_id),
    FOREIGN KEY(parent_message_id) REFERENCES peer_messages(message_id)
);

CREATE TABLE IF NOT EXISTS peer_receipts (
    message_id              TEXT NOT NULL,
    recipient_participant   TEXT NOT NULL,
    recipient_type          TEXT NOT NULL,
    recipient_transport     TEXT NOT NULL,
    recipient_order         INTEGER NOT NULL,
    delivered_ts            TEXT,
    acked_ts                TEXT,
    PRIMARY KEY(message_id, recipient_participant),
    FOREIGN KEY(message_id) REFERENCES peer_messages(message_id)
);

CREATE TABLE IF NOT EXISTS peer_events (
    event_id       TEXT PRIMARY KEY,
    message_id     TEXT,
    participant_id TEXT NOT NULL,
    event_kind     TEXT NOT NULL,
    event_ts       TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES peer_messages(message_id)
);
"""

# peer_events is the canonical append-only audit log for peer delivery state.
# It is load-bearing for operator transcript visibility; event writes stay in
# peer_store atomic helpers so state changes and audit evidence commit together.
_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_peer_messages_thread_order
    ON peer_messages(thread_id, sent_ts, message_id);
CREATE INDEX IF NOT EXISTS idx_peer_receipts_recipient_unacked
    ON peer_receipts(recipient_participant, acked_ts, message_id);
CREATE INDEX IF NOT EXISTS idx_peer_receipts_message_order
    ON peer_receipts(message_id, recipient_order);
CREATE INDEX IF NOT EXISTS idx_peer_events_message_order
    ON peer_events(message_id, event_ts, event_id);
"""

_TRIGGER_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS peer_messages_no_update
BEFORE UPDATE ON peer_messages
BEGIN
    SELECT RAISE(ABORT, 'peer_messages are immutable');
END;

CREATE TRIGGER IF NOT EXISTS peer_messages_no_delete
BEFORE DELETE ON peer_messages
BEGIN
    SELECT RAISE(ABORT, 'peer_messages are immutable');
END;

CREATE TRIGGER IF NOT EXISTS peer_events_no_update
BEFORE UPDATE ON peer_events
BEGIN
    SELECT RAISE(ABORT, 'peer_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS peer_events_no_delete
BEFORE DELETE ON peer_events
BEGIN
    SELECT RAISE(ABORT, 'peer_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS peer_receipts_state_guard
BEFORE UPDATE ON peer_receipts
BEGIN
    SELECT CASE
        WHEN OLD.delivered_ts IS NOT NULL AND NEW.delivered_ts IS NOT OLD.delivered_ts
        THEN RAISE(ABORT, 'peer_receipts delivered_ts is immutable once set')
    END;
    SELECT CASE
        WHEN OLD.acked_ts IS NOT NULL AND NEW.acked_ts IS NOT OLD.acked_ts
        THEN RAISE(ABORT, 'peer_receipts acked_ts is immutable once set')
    END;
    SELECT CASE
        WHEN NEW.acked_ts IS NOT NULL AND NEW.delivered_ts IS NULL
        THEN RAISE(ABORT, 'peer_receipts ack requires delivered')
    END;
END;
"""


def init_peer_schema(conn: sqlite3.Connection) -> None:
    """Apply the peer messaging schema and DB-level invariants. Idempotent."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_TABLE_SCHEMA)
    if _needs_shape_migration(conn):
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            _migrate_peer_messages_shape(conn)
            _migrate_peer_receipts_shape(conn)
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    else:
        _migrate_peer_messages_shape(conn)
        _migrate_peer_receipts_shape(conn)
    conn.executescript(_INDEX_SCHEMA)
    conn.executescript(_TRIGGER_SCHEMA)
    conn.commit()


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _needs_shape_migration(conn: sqlite3.Connection) -> bool:
    message_cols = _table_columns(conn, "peer_messages")
    receipt_cols = _table_columns(conn, "peer_receipts")
    return bool(
        {"to_participant", "to_participant_type", "to_transport_type"}.intersection(message_cols)
        or "read_ts" in receipt_cols
        or "recipient_order" not in receipt_cols
    )


def _migrate_peer_messages_shape(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "peer_messages")
    deprecated = {"to_participant", "to_participant_type", "to_transport_type"}
    if not deprecated.intersection(cols):
        return
    conn.executescript("""
        CREATE TABLE peer_messages_new (
            message_id             TEXT PRIMARY KEY,
            thread_id              TEXT NOT NULL,
            from_participant       TEXT NOT NULL,
            from_participant_type  TEXT NOT NULL,
            from_transport_type    TEXT NOT NULL,
            kind                   TEXT NOT NULL,
            payload_json           TEXT NOT NULL,
            content_text           TEXT NOT NULL,
            correlation_id         TEXT NOT NULL,
            parent_message_id      TEXT,
            sent_ts                TEXT NOT NULL,
            FOREIGN KEY(thread_id) REFERENCES peer_threads(thread_id),
            FOREIGN KEY(parent_message_id) REFERENCES peer_messages(message_id)
        );
        INSERT INTO peer_messages_new
              (message_id, thread_id, from_participant, from_participant_type,
               from_transport_type, kind, payload_json, content_text,
               correlation_id, parent_message_id, sent_ts)
        SELECT message_id, thread_id, from_participant, from_participant_type,
               from_transport_type, kind, payload_json, content_text,
               correlation_id, parent_message_id, sent_ts
          FROM peer_messages;
        DROP TABLE peer_messages;
        ALTER TABLE peer_messages_new RENAME TO peer_messages;
        """)


def _migrate_peer_receipts_shape(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "peer_receipts")
    if "read_ts" not in cols and "recipient_order" in cols:
        return
    conn.executescript("""
        CREATE TABLE peer_receipts_new (
            message_id              TEXT NOT NULL,
            recipient_participant   TEXT NOT NULL,
            recipient_type          TEXT NOT NULL,
            recipient_transport     TEXT NOT NULL,
            recipient_order         INTEGER NOT NULL,
            delivered_ts            TEXT,
            acked_ts                TEXT,
            PRIMARY KEY(message_id, recipient_participant),
            FOREIGN KEY(message_id) REFERENCES peer_messages(message_id)
        );
        INSERT INTO peer_receipts_new
              (message_id, recipient_participant, recipient_type,
               recipient_transport, recipient_order, delivered_ts, acked_ts)
        SELECT message_id, recipient_participant, recipient_type,
               recipient_transport,
               ROW_NUMBER() OVER (
                   PARTITION BY message_id ORDER BY recipient_participant ASC
               ) - 1,
               delivered_ts,
               acked_ts
          FROM peer_receipts;
        DROP TABLE peer_receipts;
        ALTER TABLE peer_receipts_new RENAME TO peer_receipts;
        """)


def utc_now() -> str:
    """Return an explicit UTC timestamp for peer persistence."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid.uuid4())


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(row)


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def create_thread(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    subject: str,
    created_ts: str,
) -> dict[str, Any]:
    _insert_thread_no_commit(
        conn,
        thread_id=thread_id,
        subject=subject,
        created_ts=created_ts,
    )
    conn.commit()
    thread = get_thread(conn, thread_id)
    if thread is None:
        raise RuntimeError("peer thread insert succeeded but row was not found")
    return thread


def _insert_thread_no_commit(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    subject: str,
    created_ts: str,
) -> None:
    conn.execute(
        """INSERT INTO peer_threads (thread_id, created_ts, subject, status)
           VALUES (?, ?, ?, 'open')""",
        (thread_id, created_ts, subject),
    )


def get_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM peer_threads WHERE thread_id = ?", (thread_id,)))


def list_threads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute("""SELECT t.*,
                      COUNT(m.message_id) AS message_count,
                      COALESCE(MAX(m.sent_ts), t.created_ts) AS last_activity_ts
                 FROM peer_threads t
            LEFT JOIN peer_messages m ON m.thread_id = t.thread_id
             GROUP BY t.thread_id
             ORDER BY last_activity_ts DESC, t.thread_id ASC""")
    )


def insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    thread_id: str,
    from_participant: str,
    from_participant_type: str,
    from_transport_type: str,
    kind: str,
    payload_json: dict[str, Any],
    content_text: str,
    correlation_id: str,
    parent_message_id: str | None,
    sent_ts: str,
) -> dict[str, Any]:
    _insert_message_no_commit(
        conn,
        message_id=message_id,
        thread_id=thread_id,
        from_participant=from_participant,
        from_participant_type=from_participant_type,
        from_transport_type=from_transport_type,
        kind=kind,
        payload_json=payload_json,
        content_text=content_text,
        correlation_id=correlation_id,
        parent_message_id=parent_message_id,
        sent_ts=sent_ts,
    )
    conn.commit()
    message = get_message(conn, message_id)
    if message is None:
        raise RuntimeError("peer message insert succeeded but row was not found")
    return message


def _insert_message_no_commit(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    thread_id: str,
    from_participant: str,
    from_participant_type: str,
    from_transport_type: str,
    kind: str,
    payload_json: dict[str, Any],
    content_text: str,
    correlation_id: str,
    parent_message_id: str | None,
    sent_ts: str,
) -> None:
    conn.execute(
        """INSERT INTO peer_messages
              (message_id, thread_id, from_participant, from_participant_type,
               from_transport_type, kind, payload_json, content_text,
               correlation_id, parent_message_id, sent_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            thread_id,
            from_participant,
            from_participant_type,
            from_transport_type,
            kind,
            _json_dump(payload_json),
            content_text,
            correlation_id,
            parent_message_id,
            sent_ts,
        ),
    )


def get_message(conn: sqlite3.Connection, message_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM peer_messages WHERE message_id = ?", (message_id,)))


def get_message_thread(conn: sqlite3.Connection, message_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT t.*
                 FROM peer_threads t
                 JOIN peer_messages m ON m.thread_id = t.thread_id
                WHERE m.message_id = ?""",
            (message_id,),
        )
    )


def list_thread_messages(conn: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM peer_messages
                WHERE thread_id = ?
             ORDER BY sent_ts ASC, message_id ASC""",
            (thread_id,),
        )
    )


def insert_receipt(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    recipient_type: str,
    recipient_transport: str,
    recipient_order: int,
) -> dict[str, Any]:
    _insert_receipt_no_commit(
        conn,
        message_id=message_id,
        recipient_participant=recipient_participant,
        recipient_type=recipient_type,
        recipient_transport=recipient_transport,
        recipient_order=recipient_order,
    )
    conn.commit()
    receipt = get_receipt(conn, message_id, recipient_participant)
    if receipt is None:
        raise RuntimeError("peer receipt insert succeeded but row was not found")
    return receipt


def _insert_receipt_no_commit(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    recipient_type: str,
    recipient_transport: str,
    recipient_order: int,
) -> None:
    conn.execute(
        """INSERT INTO peer_receipts
              (message_id, recipient_participant, recipient_type,
               recipient_transport, recipient_order)
           VALUES (?, ?, ?, ?, ?)""",
        (
            message_id,
            recipient_participant,
            recipient_type,
            recipient_transport,
            recipient_order,
        ),
    )


def get_receipt(
    conn: sqlite3.Connection,
    message_id: str,
    recipient_participant: str,
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM peer_receipts
                WHERE message_id = ? AND recipient_participant = ?""",
            (message_id, recipient_participant),
        )
    )


def list_receipts_for_message(conn: sqlite3.Connection, message_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM peer_receipts
                WHERE message_id = ?
             ORDER BY recipient_order ASC, recipient_participant ASC""",
            (message_id,),
        )
    )


def list_receipts_for_messages(
    conn: sqlite3.Connection,
    message_ids: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    ids = tuple(dict.fromkeys(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = _rows(
        conn.execute(
            f"""SELECT * FROM peer_receipts
                 WHERE message_id IN ({placeholders})
              ORDER BY message_id ASC, recipient_order ASC, recipient_participant ASC""",
            ids,
        )
    )
    grouped = {message_id: [] for message_id in ids}
    for row in rows:
        grouped[row["message_id"]].append(row)
    return grouped


def list_unacked_for_recipient(
    conn: sqlite3.Connection,
    recipient_participant: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT m.*
                 FROM peer_messages m
                 JOIN peer_receipts r ON r.message_id = m.message_id
                WHERE r.recipient_participant = ?
                  AND r.acked_ts IS NULL
             ORDER BY m.sent_ts ASC, m.message_id ASC
                LIMIT ?""",
            (recipient_participant, limit),
        )
    )


def mark_delivered(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    delivered_ts: str,
) -> dict[str, Any]:
    receipt = _mark_delivered_no_commit(
        conn,
        message_id=message_id,
        recipient_participant=recipient_participant,
        delivered_ts=delivered_ts,
    )
    conn.commit()
    return receipt


def _mark_delivered_no_commit(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    delivered_ts: str,
) -> dict[str, Any]:
    conn.execute(
        """UPDATE peer_receipts
              SET delivered_ts = COALESCE(delivered_ts, ?)
            WHERE message_id = ? AND recipient_participant = ?""",
        (delivered_ts, message_id, recipient_participant),
    )
    receipt = get_receipt(conn, message_id, recipient_participant)
    if receipt is None:
        raise ValueError(f"receipt not found for {message_id!r}/{recipient_participant!r}")
    return receipt


def mark_acked(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    acked_ts: str,
) -> tuple[dict[str, Any], bool]:
    receipt, changed = _mark_acked_no_commit(
        conn,
        message_id=message_id,
        recipient_participant=recipient_participant,
        acked_ts=acked_ts,
    )
    conn.commit()
    return receipt, changed


def _mark_acked_no_commit(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    acked_ts: str,
) -> tuple[dict[str, Any], bool]:
    receipt = get_receipt(conn, message_id, recipient_participant)
    if receipt is None:
        raise ValueError(f"receipt not found for {message_id!r}/{recipient_participant!r}")
    if receipt["acked_ts"] is not None:
        return receipt, False
    delivered_ts = receipt["delivered_ts"] or acked_ts
    conn.execute(
        """UPDATE peer_receipts
              SET delivered_ts = ?, acked_ts = ?
            WHERE message_id = ? AND recipient_participant = ?""",
        (delivered_ts, acked_ts, message_id, recipient_participant),
    )
    updated = get_receipt(conn, message_id, recipient_participant)
    if updated is None:
        raise RuntimeError("peer receipt disappeared during ack")
    return updated, True


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    message_id: str | None,
    participant_id: str,
    event_kind: str,
    event_ts: str,
    detail_json: dict[str, Any],
) -> dict[str, Any]:
    _insert_event_no_commit(
        conn,
        event_id=event_id,
        message_id=message_id,
        participant_id=participant_id,
        event_kind=event_kind,
        event_ts=event_ts,
        detail_json=detail_json,
    )
    conn.commit()
    event = _row(conn.execute("SELECT * FROM peer_events WHERE event_id = ?", (event_id,)))
    if event is None:
        raise RuntimeError("peer event insert succeeded but row was not found")
    return event


def list_events_for_message(conn: sqlite3.Connection, message_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM peer_events
                WHERE message_id = ?
             ORDER BY event_ts ASC, event_id ASC""",
            (message_id,),
        )
    )


def list_events_for_messages(
    conn: sqlite3.Connection,
    message_ids: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    ids = tuple(dict.fromkeys(message_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = _rows(
        conn.execute(
            f"""SELECT * FROM peer_events
                 WHERE message_id IN ({placeholders})
              ORDER BY message_id ASC, event_ts ASC, event_id ASC""",
            ids,
        )
    )
    grouped = {message_id: [] for message_id in ids}
    for row in rows:
        grouped[row["message_id"]].append(row)
    return grouped


def _insert_event_no_commit(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    message_id: str | None,
    participant_id: str,
    event_kind: str,
    event_ts: str,
    detail_json: dict[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO peer_events
              (event_id, message_id, participant_id, event_kind, event_ts, detail_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, message_id, participant_id, event_kind, event_ts, _json_dump(detail_json)),
    )


def send_message(
    conn: sqlite3.Connection,
    *,
    create_thread_args: dict[str, Any] | None,
    message_args: dict[str, Any],
    receipt_args: Sequence[dict[str, Any]],
    event_args: dict[str, Any],
) -> dict[str, Any]:
    """Atomically persist thread/message/receipts/audit as one send unit."""
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        if create_thread_args is not None:
            _insert_thread_no_commit(conn, **create_thread_args)
        _insert_message_no_commit(conn, **message_args)
        for receipt in receipt_args:
            _insert_receipt_no_commit(conn, **receipt)
        _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    message = get_message(conn, message_args["message_id"])
    if message is None:
        raise RuntimeError("atomic peer send committed but message was not found")
    return message


def poll_one_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    delivered_ts: str,
    event_args: dict[str, Any],
) -> dict[str, Any]:
    """Atomically mark a message delivered and record the poll audit event."""
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """UPDATE peer_receipts
                  SET delivered_ts = COALESCE(delivered_ts, ?)
                WHERE message_id = ? AND recipient_participant = ?""",
            (delivered_ts, message_id, recipient_participant),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"receipt not found for {message_id!r}/{recipient_participant!r}")
        _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    message = get_message(conn, message_id)
    if message is None:
        raise RuntimeError("atomic peer poll committed but message was not found")
    return message


def ack_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    acked_ts: str,
    event_args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Atomically mark a message acknowledged and record the ack audit event."""
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        receipt, changed = _mark_acked_no_commit(
            conn,
            message_id=message_id,
            recipient_participant=recipient_participant,
            acked_ts=acked_ts,
        )
        if changed:
            _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return receipt, changed


def mark_message_read(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    recipient_participant: str,
    event_args: dict[str, Any],
) -> dict[str, Any]:
    """Record operator-read visibility only after verifying recipient membership."""
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        receipt = get_receipt(conn, message_id, recipient_participant)
        if receipt is None:
            raise ValueError(f"receipt not found for {message_id!r}/{recipient_participant!r}")
        _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return receipt
