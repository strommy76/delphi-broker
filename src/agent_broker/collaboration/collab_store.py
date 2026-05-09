"""
SQLite persistence for operator-mediated collaboration.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS collab_threads (
    thread_id  TEXT PRIMARY KEY,
    created_ts TEXT NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK(status IN ('open', 'closed', 'archived'))
);

CREATE TABLE IF NOT EXISTS collab_drafts (
    draft_id               TEXT PRIMARY KEY,
    thread_id              TEXT NOT NULL,
    from_participant       TEXT NOT NULL,
    from_participant_type  TEXT NOT NULL,
    from_transport_type    TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    content_text           TEXT NOT NULL,
    correlation_id         TEXT NOT NULL,
    created_ts             TEXT NOT NULL,
    UNIQUE(from_participant, correlation_id),
    FOREIGN KEY(thread_id) REFERENCES collab_threads(thread_id)
);

CREATE TABLE IF NOT EXISTS collab_draft_recipients (
    draft_id                TEXT NOT NULL,
    recipient_participant   TEXT NOT NULL,
    recipient_type          TEXT NOT NULL,
    recipient_transport     TEXT NOT NULL,
    recipient_order         INTEGER NOT NULL,
    PRIMARY KEY(draft_id, recipient_participant),
    FOREIGN KEY(draft_id) REFERENCES collab_drafts(draft_id)
);

CREATE TABLE IF NOT EXISTS collab_operator_decisions (
    decision_id            TEXT PRIMARY KEY,
    draft_id               TEXT NOT NULL UNIQUE,
    operator_participant   TEXT NOT NULL,
    decision_type          TEXT NOT NULL CHECK(
        decision_type IN (
            'approve',
            'edit_and_approve',
            'redirect_and_approve',
            'reject'
        )
    ),
    final_payload_json     TEXT,
    final_content_text     TEXT,
    reason                 TEXT,
    decision_ts            TEXT NOT NULL,
    FOREIGN KEY(draft_id) REFERENCES collab_drafts(draft_id)
);

CREATE TABLE IF NOT EXISTS collab_decision_recipients (
    decision_id             TEXT NOT NULL,
    recipient_participant   TEXT NOT NULL,
    recipient_type          TEXT NOT NULL,
    recipient_transport     TEXT NOT NULL,
    recipient_order         INTEGER NOT NULL,
    PRIMARY KEY(decision_id, recipient_participant),
    FOREIGN KEY(decision_id) REFERENCES collab_operator_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS collab_deliverables (
    deliverable_id         TEXT PRIMARY KEY,
    draft_id               TEXT NOT NULL,
    decision_id            TEXT NOT NULL UNIQUE,
    thread_id              TEXT NOT NULL,
    from_participant       TEXT NOT NULL,
    from_participant_type  TEXT NOT NULL,
    from_transport_type    TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    content_text           TEXT NOT NULL,
    correlation_id         TEXT NOT NULL,
    created_ts             TEXT NOT NULL,
    FOREIGN KEY(draft_id) REFERENCES collab_drafts(draft_id),
    FOREIGN KEY(decision_id) REFERENCES collab_operator_decisions(decision_id),
    FOREIGN KEY(thread_id) REFERENCES collab_threads(thread_id)
);

CREATE TABLE IF NOT EXISTS collab_receipts (
    deliverable_id          TEXT NOT NULL,
    recipient_participant   TEXT NOT NULL,
    recipient_type          TEXT NOT NULL,
    recipient_transport     TEXT NOT NULL,
    recipient_order         INTEGER NOT NULL,
    delivered_ts            TEXT,
    acked_ts                TEXT,
    PRIMARY KEY(deliverable_id, recipient_participant),
    FOREIGN KEY(deliverable_id) REFERENCES collab_deliverables(deliverable_id)
);

CREATE TABLE IF NOT EXISTS collab_events (
    event_id       TEXT PRIMARY KEY,
    draft_id       TEXT,
    decision_id    TEXT,
    deliverable_id TEXT,
    participant_id TEXT NOT NULL,
    event_kind     TEXT NOT NULL,
    event_ts       TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    FOREIGN KEY(draft_id) REFERENCES collab_drafts(draft_id),
    FOREIGN KEY(decision_id) REFERENCES collab_operator_decisions(decision_id),
    FOREIGN KEY(deliverable_id) REFERENCES collab_deliverables(deliverable_id)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_collab_drafts_thread_order
    ON collab_drafts(thread_id, created_ts, draft_id);
CREATE INDEX IF NOT EXISTS idx_collab_drafts_pending
    ON collab_drafts(created_ts, draft_id);
CREATE INDEX IF NOT EXISTS idx_collab_deliverables_thread_order
    ON collab_deliverables(thread_id, created_ts, deliverable_id);
CREATE INDEX IF NOT EXISTS idx_collab_receipts_recipient_unacked
    ON collab_receipts(recipient_participant, acked_ts, deliverable_id);
CREATE INDEX IF NOT EXISTS idx_collab_events_thread_lookup
    ON collab_events(draft_id, decision_id, deliverable_id, event_ts, event_id);
"""

_TRIGGER_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS collab_drafts_no_update
BEFORE UPDATE ON collab_drafts
BEGIN
    SELECT RAISE(ABORT, 'collab_drafts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS collab_drafts_no_delete
BEFORE DELETE ON collab_drafts
BEGIN
    SELECT RAISE(ABORT, 'collab_drafts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS collab_operator_decisions_no_update
BEFORE UPDATE ON collab_operator_decisions
BEGIN
    SELECT RAISE(ABORT, 'collab_operator_decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS collab_operator_decisions_no_delete
BEFORE DELETE ON collab_operator_decisions
BEGIN
    SELECT RAISE(ABORT, 'collab_operator_decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS collab_deliverables_require_approval
BEFORE INSERT ON collab_deliverables
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
              FROM collab_operator_decisions d
             WHERE d.decision_id = NEW.decision_id
               AND d.draft_id = NEW.draft_id
               AND d.decision_type IN (
                   'approve',
                   'edit_and_approve',
                   'redirect_and_approve'
               )
        )
        THEN RAISE(ABORT, 'collab_deliverable requires approved decision')
    END;
END;

CREATE TRIGGER IF NOT EXISTS collab_deliverables_no_update
BEFORE UPDATE ON collab_deliverables
BEGIN
    SELECT RAISE(ABORT, 'collab_deliverables are immutable');
END;

CREATE TRIGGER IF NOT EXISTS collab_deliverables_no_delete
BEFORE DELETE ON collab_deliverables
BEGIN
    SELECT RAISE(ABORT, 'collab_deliverables are immutable');
END;

CREATE TRIGGER IF NOT EXISTS collab_events_no_update
BEFORE UPDATE ON collab_events
BEGIN
    SELECT RAISE(ABORT, 'collab_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS collab_events_no_delete
BEFORE DELETE ON collab_events
BEGIN
    SELECT RAISE(ABORT, 'collab_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS collab_receipts_state_guard
BEFORE UPDATE ON collab_receipts
BEGIN
    SELECT CASE
        WHEN OLD.delivered_ts IS NOT NULL AND NEW.delivered_ts IS NOT OLD.delivered_ts
        THEN RAISE(ABORT, 'collab_receipts delivered_ts is immutable once set')
    END;
    SELECT CASE
        WHEN OLD.acked_ts IS NOT NULL AND NEW.acked_ts IS NOT OLD.acked_ts
        THEN RAISE(ABORT, 'collab_receipts acked_ts is immutable once set')
    END;
    SELECT CASE
        WHEN NEW.acked_ts IS NOT NULL AND NEW.delivered_ts IS NULL
        THEN RAISE(ABORT, 'collab_receipts ack requires delivered')
    END;
END;
"""


def init_collab_schema(conn: sqlite3.Connection) -> None:
    """Apply the collaboration schema and DB-level invariants. Idempotent."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_TABLE_SCHEMA)
    conn.executescript(_INDEX_SCHEMA)
    conn.executescript(_TRIGGER_SCHEMA)
    conn.commit()


def utc_now() -> str:
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


def _json_load(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("stored JSON payload must be an object")
    return loaded


def _insert_thread_no_commit(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    subject: str,
    created_ts: str,
) -> None:
    conn.execute(
        """INSERT INTO collab_threads (thread_id, created_ts, subject, status)
           VALUES (?, ?, ?, 'open')""",
        (thread_id, created_ts, subject),
    )


def get_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM collab_threads WHERE thread_id = ?", (thread_id,)))


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM collab_drafts WHERE draft_id = ?", (draft_id,)))


def get_draft_by_idempotency(
    conn: sqlite3.Connection,
    *,
    from_participant: str,
    correlation_id: str,
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM collab_drafts
                WHERE from_participant = ? AND correlation_id = ?""",
            (from_participant, correlation_id),
        )
    )


def list_draft_recipients(conn: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM collab_draft_recipients
                WHERE draft_id = ?
             ORDER BY recipient_order ASC, recipient_participant ASC""",
            (draft_id,),
        )
    )


def list_decision_recipients(conn: sqlite3.Connection, decision_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM collab_decision_recipients
                WHERE decision_id = ?
             ORDER BY recipient_order ASC, recipient_participant ASC""",
            (decision_id,),
        )
    )


def _insert_draft_no_commit(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    thread_id: str,
    from_participant: str,
    from_participant_type: str,
    from_transport_type: str,
    kind: str,
    payload_json: dict[str, Any],
    content_text: str,
    correlation_id: str,
    created_ts: str,
) -> None:
    conn.execute(
        """INSERT INTO collab_drafts
              (draft_id, thread_id, from_participant, from_participant_type,
               from_transport_type, kind, payload_json, content_text,
               correlation_id, created_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft_id,
            thread_id,
            from_participant,
            from_participant_type,
            from_transport_type,
            kind,
            _json_dump(payload_json),
            content_text,
            correlation_id,
            created_ts,
        ),
    )


def _insert_draft_recipient_no_commit(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    recipient_participant: str,
    recipient_type: str,
    recipient_transport: str,
    recipient_order: int,
) -> None:
    conn.execute(
        """INSERT INTO collab_draft_recipients
              (draft_id, recipient_participant, recipient_type,
               recipient_transport, recipient_order)
           VALUES (?, ?, ?, ?, ?)""",
        (
            draft_id,
            recipient_participant,
            recipient_type,
            recipient_transport,
            recipient_order,
        ),
    )


def _insert_decision_recipient_no_commit(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    recipient_participant: str,
    recipient_type: str,
    recipient_transport: str,
    recipient_order: int,
) -> None:
    conn.execute(
        """INSERT INTO collab_decision_recipients
              (decision_id, recipient_participant, recipient_type,
               recipient_transport, recipient_order)
           VALUES (?, ?, ?, ?, ?)""",
        (
            decision_id,
            recipient_participant,
            recipient_type,
            recipient_transport,
            recipient_order,
        ),
    )


def _insert_event_no_commit(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    draft_id: str | None,
    decision_id: str | None,
    deliverable_id: str | None,
    participant_id: str,
    event_kind: str,
    event_ts: str,
    detail_json: dict[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO collab_events
              (event_id, draft_id, decision_id, deliverable_id,
               participant_id, event_kind, event_ts, detail_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            draft_id,
            decision_id,
            deliverable_id,
            participant_id,
            event_kind,
            event_ts,
            _json_dump(detail_json),
        ),
    )


def create_draft(
    conn: sqlite3.Connection,
    *,
    create_thread_args: dict[str, Any] | None,
    draft_args: dict[str, Any],
    recipient_args: Sequence[dict[str, Any]],
    event_args: dict[str, Any],
) -> dict[str, Any]:
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        if create_thread_args is not None:
            _insert_thread_no_commit(conn, **create_thread_args)
        _insert_draft_no_commit(conn, **draft_args)
        for recipient in recipient_args:
            _insert_draft_recipient_no_commit(conn, **recipient)
        _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    draft = get_draft(conn, draft_args["draft_id"])
    if draft is None:
        raise RuntimeError("collaboration draft committed but row was not found")
    return draft


def get_decision_for_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM collab_operator_decisions WHERE draft_id = ?""",
            (draft_id,),
        )
    )


def get_decision(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM collab_operator_decisions WHERE decision_id = ?""",
            (decision_id,),
        )
    )


def _insert_decision_no_commit(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    draft_id: str,
    operator_participant: str,
    decision_type: str,
    final_payload_json: dict[str, Any] | None,
    final_content_text: str | None,
    reason: str | None,
    decision_ts: str,
) -> None:
    conn.execute(
        """INSERT INTO collab_operator_decisions
              (decision_id, draft_id, operator_participant, decision_type,
               final_payload_json, final_content_text, reason, decision_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            decision_id,
            draft_id,
            operator_participant,
            decision_type,
            None if final_payload_json is None else _json_dump(final_payload_json),
            final_content_text,
            reason,
            decision_ts,
        ),
    )


def _insert_deliverable_no_commit(
    conn: sqlite3.Connection,
    *,
    deliverable_id: str,
    draft_id: str,
    decision_id: str,
    thread_id: str,
    from_participant: str,
    from_participant_type: str,
    from_transport_type: str,
    kind: str,
    payload_json: dict[str, Any],
    content_text: str,
    correlation_id: str,
    created_ts: str,
) -> None:
    conn.execute(
        """INSERT INTO collab_deliverables
              (deliverable_id, draft_id, decision_id, thread_id,
               from_participant, from_participant_type, from_transport_type,
               kind, payload_json, content_text, correlation_id, created_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            deliverable_id,
            draft_id,
            decision_id,
            thread_id,
            from_participant,
            from_participant_type,
            from_transport_type,
            kind,
            _json_dump(payload_json),
            content_text,
            correlation_id,
            created_ts,
        ),
    )


def _insert_receipt_no_commit(
    conn: sqlite3.Connection,
    *,
    deliverable_id: str,
    recipient_participant: str,
    recipient_type: str,
    recipient_transport: str,
    recipient_order: int,
) -> None:
    conn.execute(
        """INSERT INTO collab_receipts
              (deliverable_id, recipient_participant, recipient_type,
               recipient_transport, recipient_order)
           VALUES (?, ?, ?, ?, ?)""",
        (
            deliverable_id,
            recipient_participant,
            recipient_type,
            recipient_transport,
            recipient_order,
        ),
    )


def record_decision(
    conn: sqlite3.Connection,
    *,
    decision_args: dict[str, Any],
    decision_recipient_args: Sequence[dict[str, Any]],
    deliverable_args: dict[str, Any] | None,
    receipt_args: Sequence[dict[str, Any]],
    decision_event_args: dict[str, Any],
    deliverable_event_args: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _insert_decision_no_commit(conn, **decision_args)
        for recipient in decision_recipient_args:
            _insert_decision_recipient_no_commit(conn, **recipient)
        _insert_event_no_commit(conn, **decision_event_args)
        if deliverable_args is not None:
            _insert_deliverable_no_commit(conn, **deliverable_args)
            for receipt in receipt_args:
                _insert_receipt_no_commit(conn, **receipt)
            if deliverable_event_args is not None:
                _insert_event_no_commit(conn, **deliverable_event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    decision = get_decision(conn, decision_args["decision_id"])
    if decision is None:
        raise RuntimeError("collaboration decision committed but row was not found")
    deliverable = (
        None
        if deliverable_args is None
        else get_deliverable(conn, deliverable_args["deliverable_id"])
    )
    if deliverable_args is not None and deliverable is None:
        raise RuntimeError("collaboration deliverable committed but row was not found")
    return decision, deliverable


def get_deliverable(conn: sqlite3.Connection, deliverable_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM collab_deliverables WHERE deliverable_id = ?""",
            (deliverable_id,),
        )
    )


def get_deliverable_for_decision(
    conn: sqlite3.Connection,
    decision_id: str,
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM collab_deliverables WHERE decision_id = ?""",
            (decision_id,),
        )
    )


def list_receipts_for_deliverable(
    conn: sqlite3.Connection,
    deliverable_id: str,
) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM collab_receipts
                WHERE deliverable_id = ?
             ORDER BY recipient_order ASC, recipient_participant ASC""",
            (deliverable_id,),
        )
    )


def get_receipt(
    conn: sqlite3.Connection,
    deliverable_id: str,
    recipient_participant: str,
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """SELECT * FROM collab_receipts
                WHERE deliverable_id = ? AND recipient_participant = ?""",
            (deliverable_id, recipient_participant),
        )
    )


def list_unacked_for_recipient(
    conn: sqlite3.Connection,
    recipient_participant: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT d.*
                 FROM collab_deliverables d
                 JOIN collab_receipts r ON r.deliverable_id = d.deliverable_id
                WHERE r.recipient_participant = ?
                  AND r.acked_ts IS NULL
             ORDER BY d.created_ts ASC, d.deliverable_id ASC
                LIMIT ?""",
            (recipient_participant, limit),
        )
    )


def mark_delivered(
    conn: sqlite3.Connection,
    *,
    deliverable_id: str,
    recipient_participant: str,
    delivered_ts: str,
    event_args: dict[str, Any],
) -> dict[str, Any]:
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """UPDATE collab_receipts
                  SET delivered_ts = COALESCE(delivered_ts, ?)
                WHERE deliverable_id = ? AND recipient_participant = ?""",
            (delivered_ts, deliverable_id, recipient_participant),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"receipt not found for {deliverable_id!r}/{recipient_participant!r}")
        _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    deliverable = get_deliverable(conn, deliverable_id)
    if deliverable is None:
        raise RuntimeError("collaboration deliverable disappeared during poll")
    return deliverable


def mark_acked(
    conn: sqlite3.Connection,
    *,
    deliverable_id: str,
    recipient_participant: str,
    acked_ts: str,
    event_args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        receipt = get_receipt(conn, deliverable_id, recipient_participant)
        if receipt is None:
            raise ValueError(f"receipt not found for {deliverable_id!r}/{recipient_participant!r}")
        if receipt["delivered_ts"] is None:
            raise ValueError(
                f"deliverable {deliverable_id!r} has not been delivered "
                f"to {recipient_participant!r}"
            )
        if receipt["acked_ts"] is not None:
            conn.commit()
            return receipt, False
        conn.execute(
            """UPDATE collab_receipts
                  SET acked_ts = ?
                WHERE deliverable_id = ? AND recipient_participant = ?""",
            (acked_ts, deliverable_id, recipient_participant),
        )
        _insert_event_no_commit(conn, **event_args)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    updated = get_receipt(conn, deliverable_id, recipient_participant)
    if updated is None:
        raise RuntimeError("collaboration receipt disappeared during ack")
    return updated, True


def list_pending_drafts(conn: sqlite3.Connection, *, include_probes: bool) -> list[dict[str, Any]]:
    # Probe filtering is completed by the service from participant metadata so
    # this store query stays independent of identity policy.
    del include_probes
    return _rows(
        conn.execute(
            """SELECT d.*
                 FROM collab_drafts d
            LEFT JOIN collab_operator_decisions od ON od.draft_id = d.draft_id
                WHERE od.decision_id IS NULL
             ORDER BY d.created_ts ASC, d.draft_id ASC"""
        )
    )


def list_thread_drafts(conn: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM collab_drafts
                WHERE thread_id = ?
             ORDER BY created_ts ASC, draft_id ASC""",
            (thread_id,),
        )
    )


def list_thread_deliverables(conn: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            """SELECT * FROM collab_deliverables
                WHERE thread_id = ?
             ORDER BY created_ts ASC, deliverable_id ASC""",
            (thread_id,),
        )
    )


def list_events_for_refs(
    conn: sqlite3.Connection,
    *,
    draft_ids: Iterable[str],
    decision_ids: Iterable[str],
    deliverable_ids: Iterable[str],
) -> list[dict[str, Any]]:
    clauses = []
    params: list[str] = []
    for column, values in (
        ("draft_id", tuple(dict.fromkeys(draft_ids))),
        ("decision_id", tuple(dict.fromkeys(decision_ids))),
        ("deliverable_id", tuple(dict.fromkeys(deliverable_ids))),
    ):
        if values:
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"{column} IN ({placeholders})")
            params.extend(values)
    if not clauses:
        return []
    return _rows(
        conn.execute(
            f"""SELECT * FROM collab_events
                 WHERE {" OR ".join(clauses)}
              ORDER BY event_ts ASC, event_id ASC""",
            tuple(params),
        )
    )


def detail_from_row(row: dict[str, Any], field: str = "detail_json") -> dict[str, Any]:
    value = _json_load(row[field])
    if value is None:
        raise ValueError(f"{field} is NULL")
    return value


def payload_from_row(row: dict[str, Any], field: str = "payload_json") -> dict[str, Any]:
    value = _json_load(row[field])
    if value is None:
        raise ValueError(f"{field} is NULL")
    return value


def nullable_payload_from_row(
    row: dict[str, Any], field: str = "final_payload_json"
) -> dict[str, Any] | None:
    return _json_load(row[field])
