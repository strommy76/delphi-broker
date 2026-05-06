"""
--------------------------------------------------------------------------------
FILE:        test_peer_store.py
PATH:        ~/projects/agent-broker/tests/test_peer_store.py
DESCRIPTION: Schema migration and SQLite invariant tests for the peer messaging store.

CHANGELOG:
2026-05-06 13:34      Codex      [Fix] Assert schema init never drops peer triggers on idempotent re-entry.
2026-05-06 13:11      Codex      [Cleanup] Remove cleanup-probe artifact test after canonical cleanup path deletion.
2026-05-06 09:49      Codex      [Cleanup] Add coverage for known peer probe artifact cleanup and trigger restoration.
2026-05-06 09:47      Codex      [Refactor] Cover peer schema shape cleanup and recipient-order receipt migration.
2026-05-06 09:29      Codex      [Feature] Add Phase 4 peer store schema, FK, immutability, and ordering coverage.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_broker.peer import peer_store


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    peer_store.init_peer_schema(conn)
    return conn


def _seed_thread(conn: sqlite3.Connection, thread_id: str = "thread-1") -> dict:
    return peer_store.create_thread(
        conn,
        thread_id=thread_id,
        subject="coordination",
        created_ts="2026-05-06T13:30:00+00:00",
    )


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str = "msg-1",
    thread_id: str = "thread-1",
    sent_ts: str = "2026-05-06T13:30:01+00:00",
) -> dict:
    return peer_store.insert_message(
        conn,
        message_id=message_id,
        thread_id=thread_id,
        from_participant="pi-claude",
        from_participant_type="agent",
        from_transport_type="mcp",
        kind="text",
        payload_json={"body": message_id},
        content_text=message_id,
        correlation_id=f"corr-{message_id}",
        parent_message_id=None,
        sent_ts=sent_ts,
    )


def test_peer_schema_migration_is_idempotent_and_creates_tables():
    conn = _conn()
    try:
        peer_store.init_peer_schema(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'peer_%'"
            )
        }
        receipt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(peer_receipts)")}
        message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(peer_messages)")}
        assert tables == {"peer_threads", "peer_messages", "peer_receipts", "peer_events"}
        assert "read_ts" not in receipt_cols
        assert "recipient_order" in receipt_cols
        assert not {"to_participant", "to_participant_type", "to_transport_type"}.intersection(
            message_cols
        )
    finally:
        conn.close()


def test_peer_schema_reinit_does_not_drop_triggers():
    conn = _conn()
    traced: list[str] = []
    try:
        conn.set_trace_callback(traced.append)
        peer_store.init_peer_schema(conn)
        conn.set_trace_callback(None)
    finally:
        conn.close()

    assert not any("DROP TRIGGER" in statement.upper() for statement in traced)


def test_peer_message_round_trip_and_deterministic_ordering():
    conn = _conn()
    try:
        _seed_thread(conn)
        later = _insert_message(
            conn,
            message_id="msg-later",
            sent_ts="2026-05-06T13:30:02+00:00",
        )
        earlier = _insert_message(
            conn,
            message_id="msg-earlier",
            sent_ts="2026-05-06T13:30:01+00:00",
        )
        rows = peer_store.list_thread_messages(conn, "thread-1")
    finally:
        conn.close()

    assert later["payload_json"] == '{"body":"msg-later"}'
    assert earlier["sent_ts"].endswith("+00:00")
    assert [row["message_id"] for row in rows] == ["msg-earlier", "msg-later"]


def test_peer_message_fk_violation_raises():
    conn = _conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_message(conn, thread_id="missing-thread")
    finally:
        conn.close()


def test_peer_messages_are_immutable_by_trigger():
    conn = _conn()
    try:
        _seed_thread(conn)
        _insert_message(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE peer_messages SET content_text = ? WHERE message_id = ?",
                ("mutated", "msg-1"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM peer_messages WHERE message_id = ?", ("msg-1",))
    finally:
        conn.close()


def test_peer_events_are_append_only_by_trigger():
    conn = _conn()
    try:
        event = peer_store.insert_event(
            conn,
            event_id="event-1",
            message_id=None,
            participant_id="pi-claude",
            event_kind="schema_test",
            event_ts="2026-05-06T13:30:03+00:00",
            detail_json={"ok": True},
        )
        assert event["detail_json"] == '{"ok":true}'
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE peer_events SET event_kind = ? WHERE event_id = ?",
                ("mutated", "event-1"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM peer_events WHERE event_id = ?", ("event-1",))
    finally:
        conn.close()


def test_peer_receipt_state_transitions_are_enforced():
    conn = _conn()
    try:
        _seed_thread(conn)
        _insert_message(conn)
        peer_store.insert_receipt(
            conn,
            message_id="msg-1",
            recipient_participant="pi-codex",
            recipient_type="agent",
            recipient_transport="mcp",
            recipient_order=0,
        )
        delivered = peer_store.mark_delivered(
            conn,
            message_id="msg-1",
            recipient_participant="pi-codex",
            delivered_ts="2026-05-06T13:30:05+00:00",
        )
        assert delivered["delivered_ts"] == "2026-05-06T13:30:05+00:00"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """UPDATE peer_receipts
                      SET delivered_ts = ?
                    WHERE message_id = ? AND recipient_participant = ?""",
                ("2026-05-06T13:30:06+00:00", "msg-1", "pi-codex"),
            )
    finally:
        conn.close()


def test_legacy_peer_schema_migrates_to_current_shape():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.executescript("""
            CREATE TABLE peer_threads (
                thread_id TEXT PRIMARY KEY,
                created_ts TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE peer_messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                from_participant TEXT NOT NULL,
                from_participant_type TEXT NOT NULL,
                from_transport_type TEXT NOT NULL,
                to_participant TEXT,
                to_participant_type TEXT,
                to_transport_type TEXT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_text TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                parent_message_id TEXT,
                sent_ts TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES peer_threads(thread_id)
            );
            CREATE TABLE peer_receipts (
                message_id TEXT NOT NULL,
                recipient_participant TEXT NOT NULL,
                recipient_type TEXT NOT NULL,
                recipient_transport TEXT NOT NULL,
                delivered_ts TEXT,
                read_ts TEXT,
                acked_ts TEXT,
                PRIMARY KEY(message_id, recipient_participant),
                FOREIGN KEY(message_id) REFERENCES peer_messages(message_id)
            );
            CREATE TABLE peer_events (
                event_id TEXT PRIMARY KEY,
                message_id TEXT,
                participant_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                event_ts TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
            INSERT INTO peer_threads VALUES
                ('thread-legacy', '2026-05-06T13:30:00+00:00', 'legacy', 'open');
            INSERT INTO peer_messages
                (message_id, thread_id, from_participant, from_participant_type,
                 from_transport_type, to_participant, to_participant_type,
                 to_transport_type, kind, payload_json, content_text,
                 correlation_id, parent_message_id, sent_ts)
            VALUES
                ('msg-legacy', 'thread-legacy', 'pi-claude', 'agent', 'mcp',
                 'pi-codex', 'agent', 'mcp', 'text', '{}', 'legacy',
                 'corr-legacy', NULL, '2026-05-06T13:30:01+00:00');
            INSERT INTO peer_receipts
                (message_id, recipient_participant, recipient_type,
                 recipient_transport, delivered_ts, read_ts, acked_ts)
            VALUES
                ('msg-legacy', 'pi-codex', 'agent', 'mcp', NULL, NULL, NULL);
            """)
        peer_store.init_peer_schema(conn)
        receipt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(peer_receipts)")}
        message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(peer_messages)")}
        receipt = peer_store.get_receipt(conn, "msg-legacy", "pi-codex")
    finally:
        conn.close()

    assert "read_ts" not in receipt_cols
    assert "recipient_order" in receipt_cols
    assert not {"to_participant", "to_participant_type", "to_transport_type"}.intersection(
        message_cols
    )
    assert receipt is not None
    assert receipt["recipient_order"] == 0
