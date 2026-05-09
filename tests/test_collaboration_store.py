from __future__ import annotations

import sqlite3

import pytest

from agent_broker.collaboration import collab_store
from agent_broker.peer import peer_store


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    peer_store.init_peer_schema(conn)
    collab_store.init_collab_schema(conn)
    return conn


def test_collab_schema_adds_namespace_without_mutating_peer_tables():
    conn = _conn()
    try:
        peer_message_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(peer_messages)")
        }
        peer_receipt_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(peer_receipts)")
        }
        collab_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'collab_%'"
            )
        }
    finally:
        conn.close()

    assert peer_message_cols == {
        "message_id",
        "thread_id",
        "from_participant",
        "from_participant_type",
        "from_transport_type",
        "kind",
        "payload_json",
        "content_text",
        "correlation_id",
        "parent_message_id",
        "sent_ts",
    }
    assert peer_receipt_cols == {
        "message_id",
        "recipient_participant",
        "recipient_type",
        "recipient_transport",
        "recipient_order",
        "delivered_ts",
        "acked_ts",
    }
    assert collab_tables == {
        "collab_threads",
        "collab_drafts",
        "collab_draft_recipients",
        "collab_operator_decisions",
        "collab_decision_recipients",
        "collab_deliverables",
        "collab_receipts",
        "collab_events",
    }


def test_collab_events_are_append_only_and_drafts_are_immutable():
    conn = _conn()
    try:
        draft = collab_store.create_draft(
            conn,
            create_thread_args={
                "thread_id": "thread-1",
                "subject": "coordination",
                "created_ts": "2026-05-09T00:00:00+00:00",
            },
            draft_args={
                "draft_id": "draft-1",
                "thread_id": "thread-1",
                "from_participant": "dev-codex",
                "from_participant_type": "agent",
                "from_transport_type": "mcp",
                "kind": "text",
                "payload_json": {"body": "draft"},
                "content_text": "draft",
                "correlation_id": "corr-1",
                "created_ts": "2026-05-09T00:00:01+00:00",
            },
            recipient_args=[
                {
                    "draft_id": "draft-1",
                    "recipient_participant": "prod-codex",
                    "recipient_type": "agent",
                    "recipient_transport": "mcp",
                    "recipient_order": 0,
                }
            ],
            event_args={
                "event_id": "event-1",
                "draft_id": "draft-1",
                "decision_id": None,
                "deliverable_id": None,
                "participant_id": "dev-codex",
                "event_kind": "draft_created",
                "event_ts": "2026-05-09T00:00:01+00:00",
                "detail_json": {},
            },
        )
        assert draft["draft_id"] == "draft-1"
        with pytest.raises(sqlite3.IntegrityError, match="collab_drafts are immutable"):
            conn.execute(
                "UPDATE collab_drafts SET content_text = ? WHERE draft_id = ?",
                ("mutated", "draft-1"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="collab_events are append-only"):
            conn.execute(
                "UPDATE collab_events SET event_kind = ? WHERE event_id = ?",
                ("mutated", "event-1"),
            )
    finally:
        conn.close()


def test_collab_deliverable_requires_approved_decision():
    conn = _conn()
    try:
        conn.executescript("""
            INSERT INTO collab_threads VALUES
                ('thread-1', '2026-05-09T00:00:00+00:00', 'coordination', 'open');
            INSERT INTO collab_drafts
                (draft_id, thread_id, from_participant, from_participant_type,
                 from_transport_type, kind, payload_json, content_text,
                 correlation_id, created_ts)
            VALUES
                ('draft-1', 'thread-1', 'dev-codex', 'agent', 'mcp', 'text',
                 '{}', 'draft', 'corr-1', '2026-05-09T00:00:01+00:00');
            INSERT INTO collab_operator_decisions
                (decision_id, draft_id, operator_participant, decision_type,
                 final_payload_json, final_content_text, reason, decision_ts)
            VALUES
                ('decision-1', 'draft-1', 'operator', 'reject', NULL, NULL,
                 'no', '2026-05-09T00:00:02+00:00');
        """)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_deliverable requires approved decision",
        ):
            conn.execute(
                """INSERT INTO collab_deliverables
                      (deliverable_id, draft_id, decision_id, thread_id,
                       from_participant, from_participant_type,
                       from_transport_type, kind, payload_json, content_text,
                       correlation_id, created_ts)
                   VALUES
                      ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
                       'dev-codex', 'agent', 'mcp', 'text', '{}', 'draft',
                       'corr-1', '2026-05-09T00:00:03+00:00')"""
            )
    finally:
        conn.close()
