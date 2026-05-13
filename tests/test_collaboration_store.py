from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

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


def _seed_approved_decision(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        INSERT INTO collab_threads VALUES
            ('thread-1', '2026-05-09T00:00:00+00:00', 'coordination', 'open');
        INSERT INTO collab_drafts
            (draft_id, thread_id, from_participant, from_participant_type,
             from_transport_type, kind, payload_json, content_text,
             correlation_id, created_ts)
        VALUES
            ('draft-1', 'thread-1', 'dev-codex', 'agent', 'mcp', 'text',
             '{"body":"draft"}', 'draft', 'corr-1', '2026-05-09T00:00:01+00:00');
        INSERT INTO collab_draft_recipients
            (draft_id, recipient_participant, recipient_type,
             recipient_transport, recipient_order)
        VALUES ('draft-1', 'prod-codex', 'agent', 'mcp', 0);
        INSERT INTO collab_operator_decisions
            (decision_id, draft_id, operator_participant, decision_type,
             final_payload_json, final_content_text, reason, decision_ts)
        VALUES
            ('decision-1', 'draft-1', 'operator', 'approve',
             '{"body":"draft"}', 'draft', NULL, '2026-05-09T00:00:02+00:00');
        INSERT INTO collab_decision_recipients
            (decision_id, recipient_participant, recipient_type,
             recipient_transport, recipient_order)
        VALUES ('decision-1', 'prod-codex', 'agent', 'mcp', 0);
        INSERT INTO collab_events
            (event_id, draft_id, decision_id, deliverable_id, participant_id,
             event_kind, event_ts, detail_json)
        VALUES
            ('event-draft-1', 'draft-1', NULL, NULL, 'dev-codex',
             'draft_created', '2026-05-09T00:00:01+00:00', '{}');
        INSERT INTO collab_events
            (event_id, draft_id, decision_id, deliverable_id, participant_id,
             event_kind, event_ts, detail_json)
        VALUES
            ('event-decision-1', 'draft-1', 'decision-1', NULL, 'operator',
             'operator_approve', '2026-05-09T00:00:02+00:00', '{}');
    """
    )


def _insert_matching_deliverable(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO collab_deliverables
              (deliverable_id, draft_id, decision_id, thread_id,
               from_participant, from_participant_type, from_transport_type,
               kind, payload_json, content_text, correlation_id, created_ts)
           VALUES
              ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
               'dev-codex', 'agent', 'mcp', 'text', '{"body":"draft"}', 'draft',
               'corr-1', '2026-05-09T00:00:03+00:00')"""
    )
    conn.execute(
        """INSERT INTO collab_events
              (event_id, draft_id, decision_id, deliverable_id, participant_id,
               event_kind, event_ts, detail_json)
           VALUES
              ('event-deliverable-1', 'draft-1', 'decision-1', 'deliverable-1',
               'operator', 'deliverable_created', '2026-05-09T00:00:03+00:00', '{}')"""
    )


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


def test_collab_recipient_junctions_are_immutable():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_draft_recipients are immutable",
        ):
            conn.execute(
                """UPDATE collab_draft_recipients
                      SET recipient_order = 1
                    WHERE draft_id = 'draft-1'"""
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_draft_recipients are immutable",
        ):
            conn.execute("DELETE FROM collab_draft_recipients WHERE draft_id = 'draft-1'")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_decision_recipients are immutable",
        ):
            conn.execute(
                """UPDATE collab_decision_recipients
                      SET recipient_order = 1
                    WHERE decision_id = 'decision-1'"""
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_decision_recipients are immutable",
        ):
            conn.execute("DELETE FROM collab_decision_recipients WHERE decision_id = 'decision-1'")
    finally:
        conn.close()


def test_collab_recipient_junctions_close_after_authority_events():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_draft_recipients closed after draft creation",
        ):
            conn.execute(
                """INSERT INTO collab_draft_recipients
                      (draft_id, recipient_participant, recipient_type,
                       recipient_transport, recipient_order)
                   VALUES ('draft-1', 'flow-claude', 'agent', 'mcp', 1)"""
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_decision_recipients closed after operator decision",
        ):
            conn.execute(
                """INSERT INTO collab_decision_recipients
                      (decision_id, recipient_participant, recipient_type,
                       recipient_transport, recipient_order)
                   VALUES ('decision-1', 'flow-claude', 'agent', 'mcp', 1)"""
            )
    finally:
        conn.close()


def test_collab_deliverable_requires_approved_decision():
    conn = _conn()
    try:
        conn.executescript(
            """
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
        """
        )
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


def test_operator_initiated_deliverable_satisfies_approval_guard():
    conn = _conn()
    try:
        conn.executescript(
            """
            INSERT INTO collab_threads VALUES
                ('thread-1', '2026-05-09T00:00:00+00:00', 'operator message', 'open');
            INSERT INTO collab_drafts
                (draft_id, thread_id, from_participant, from_participant_type,
                 from_transport_type, kind, payload_json, content_text,
                 correlation_id, created_ts)
            VALUES
                ('draft-1', 'thread-1', 'operator', 'operator', 'http', 'text',
                 '{"body":"operator"}', 'operator', 'corr-operator',
                 '2026-05-09T00:00:01+00:00');
            INSERT INTO collab_draft_recipients
                (draft_id, recipient_participant, recipient_type,
                 recipient_transport, recipient_order)
            VALUES ('draft-1', 'prod-codex', 'agent', 'mcp', 0);
            INSERT INTO collab_events
                (event_id, draft_id, decision_id, deliverable_id, participant_id,
                 event_kind, event_ts, detail_json)
            VALUES
                ('event-draft-1', 'draft-1', NULL, NULL, 'operator',
                 'operator_composed', '2026-05-09T00:00:01+00:00', '{}');
            INSERT INTO collab_operator_decisions
                (decision_id, draft_id, operator_participant, decision_type,
                 final_payload_json, final_content_text, reason, decision_ts)
            VALUES
                ('decision-1', 'draft-1', 'operator', 'operator_initiated',
                 '{"body":"operator"}', 'operator', NULL,
                 '2026-05-09T00:00:02+00:00');
            INSERT INTO collab_decision_recipients
                (decision_id, recipient_participant, recipient_type,
                 recipient_transport, recipient_order)
            VALUES ('decision-1', 'prod-codex', 'agent', 'mcp', 0);
            INSERT INTO collab_events
                (event_id, draft_id, decision_id, deliverable_id, participant_id,
                 event_kind, event_ts, detail_json)
            VALUES
                ('event-decision-1', 'draft-1', 'decision-1', NULL, 'operator',
                 'operator_initiated_message', '2026-05-09T00:00:02+00:00', '{}');
        """
        )

        conn.execute(
            """INSERT INTO collab_deliverables
                  (deliverable_id, draft_id, decision_id, thread_id,
                   from_participant, from_participant_type, from_transport_type,
                   kind, payload_json, content_text, correlation_id, created_ts)
               VALUES
                  ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
                   'operator', 'operator', 'http', 'text',
                   '{"body":"operator"}', 'operator', 'corr-operator',
                   '2026-05-09T00:00:03+00:00')"""
        )
    finally:
        conn.close()


def test_operator_initiated_decision_type_migrates_existing_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE collab_threads (
                thread_id  TEXT PRIMARY KEY,
                created_ts TEXT NOT NULL,
                subject    TEXT NOT NULL,
                status     TEXT NOT NULL CHECK(status IN ('open', 'closed', 'archived'))
            );
            CREATE TABLE collab_drafts (
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
            CREATE TABLE collab_operator_decisions (
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
            INSERT INTO collab_threads VALUES
                ('thread-1', '2026-05-09T00:00:00+00:00', 'existing', 'open'),
                ('thread-2', '2026-05-09T00:01:00+00:00', 'operator', 'open');
            INSERT INTO collab_drafts
                (draft_id, thread_id, from_participant, from_participant_type,
                 from_transport_type, kind, payload_json, content_text,
                 correlation_id, created_ts)
            VALUES
                ('draft-1', 'thread-1', 'dev-codex', 'agent', 'mcp', 'text',
                 '{"body":"existing"}', 'existing', 'corr-existing',
                 '2026-05-09T00:00:01+00:00'),
                ('draft-2', 'thread-2', 'operator', 'operator', 'http', 'text',
                 '{"body":"operator"}', 'operator', 'corr-operator',
                 '2026-05-09T00:01:01+00:00');
            INSERT INTO collab_operator_decisions
                (decision_id, draft_id, operator_participant, decision_type,
                 final_payload_json, final_content_text, reason, decision_ts)
            VALUES
                ('decision-1', 'draft-1', 'operator', 'approve',
                 '{"body":"existing"}', 'existing', NULL,
                 '2026-05-09T00:00:02+00:00');
        """
        )

        collab_store.init_collab_schema(conn)

        assert collab_store.get_decision(conn, "decision-1")["decision_type"] == "approve"
        conn.execute(
            """INSERT INTO collab_operator_decisions
                  (decision_id, draft_id, operator_participant, decision_type,
                   final_payload_json, final_content_text, reason, decision_ts)
               VALUES
                  ('decision-2', 'draft-2', 'operator', 'operator_initiated',
                   '{"body":"operator"}', 'operator', NULL,
                   '2026-05-09T00:01:02+00:00')"""
        )
        assert (
            collab_store.get_decision(conn, "decision-2")["decision_type"] == "operator_initiated"
        )
    finally:
        conn.close()


def test_collab_deliverable_requires_operator_decision_event():
    conn = _conn()
    try:
        conn.executescript(
            """
            INSERT INTO collab_threads VALUES
                ('thread-1', '2026-05-09T00:00:00+00:00', 'coordination', 'open');
            INSERT INTO collab_drafts
                (draft_id, thread_id, from_participant, from_participant_type,
                 from_transport_type, kind, payload_json, content_text,
                 correlation_id, created_ts)
            VALUES
                ('draft-1', 'thread-1', 'dev-codex', 'agent', 'mcp', 'text',
                 '{"body":"draft"}', 'draft', 'corr-1', '2026-05-09T00:00:01+00:00');
            INSERT INTO collab_operator_decisions
                (decision_id, draft_id, operator_participant, decision_type,
                 final_payload_json, final_content_text, reason, decision_ts)
            VALUES
                ('decision-1', 'draft-1', 'operator', 'approve',
                 '{"body":"draft"}', 'draft', NULL, '2026-05-09T00:00:02+00:00');
        """
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_deliverable requires operator decision event",
        ):
            conn.execute(
                """INSERT INTO collab_deliverables
                      (deliverable_id, draft_id, decision_id, thread_id,
                       from_participant, from_participant_type, from_transport_type,
                       kind, payload_json, content_text, correlation_id, created_ts)
                   VALUES
                      ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
                       'dev-codex', 'agent', 'mcp', 'text',
                       '{"body":"draft"}', 'draft', 'corr-1',
                       '2026-05-09T00:00:03+00:00')"""
            )
    finally:
        conn.close()


def test_operator_message_record_rolls_back_if_decision_insert_fails():
    conn = _conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            collab_store.record_operator_message(
                conn,
                create_thread_args={
                    "thread_id": "thread-1",
                    "subject": "operator message",
                    "created_ts": "2026-05-09T00:00:00+00:00",
                },
                draft_args={
                    "draft_id": "draft-1",
                    "thread_id": "thread-1",
                    "from_participant": "operator",
                    "from_participant_type": "operator",
                    "from_transport_type": "http",
                    "kind": "text",
                    "payload_json": {"body": "operator"},
                    "content_text": "operator",
                    "correlation_id": "corr-operator",
                    "created_ts": "2026-05-09T00:00:01+00:00",
                },
                draft_recipient_args=[
                    {
                        "draft_id": "draft-1",
                        "recipient_participant": "prod-codex",
                        "recipient_type": "agent",
                        "recipient_transport": "mcp",
                        "recipient_order": 0,
                    }
                ],
                decision_args={
                    "decision_id": "decision-1",
                    "draft_id": "draft-1",
                    "operator_participant": "operator",
                    "decision_type": "not_a_decision_type",
                    "final_payload_json": {"body": "operator"},
                    "final_content_text": "operator",
                    "reason": None,
                    "decision_ts": "2026-05-09T00:00:02+00:00",
                },
                decision_recipient_args=[
                    {
                        "decision_id": "decision-1",
                        "recipient_participant": "prod-codex",
                        "recipient_type": "agent",
                        "recipient_transport": "mcp",
                        "recipient_order": 0,
                    }
                ],
                deliverable_args={
                    "deliverable_id": "deliverable-1",
                    "draft_id": "draft-1",
                    "decision_id": "decision-1",
                    "thread_id": "thread-1",
                    "from_participant": "operator",
                    "from_participant_type": "operator",
                    "from_transport_type": "http",
                    "kind": "text",
                    "payload_json": {"body": "operator"},
                    "content_text": "operator",
                    "correlation_id": "corr-operator",
                    "created_ts": "2026-05-09T00:00:03+00:00",
                },
                receipt_args=[
                    {
                        "deliverable_id": "deliverable-1",
                        "recipient_participant": "prod-codex",
                        "recipient_type": "agent",
                        "recipient_transport": "mcp",
                        "recipient_order": 0,
                    }
                ],
                draft_event_args={
                    "event_id": "event-draft-1",
                    "draft_id": "draft-1",
                    "decision_id": None,
                    "deliverable_id": None,
                    "participant_id": "operator",
                    "event_kind": "operator_composed",
                    "event_ts": "2026-05-09T00:00:01+00:00",
                    "detail_json": {},
                },
                decision_event_args={
                    "event_id": "event-decision-1",
                    "draft_id": "draft-1",
                    "decision_id": "decision-1",
                    "deliverable_id": None,
                    "participant_id": "operator",
                    "event_kind": "operator_initiated_message",
                    "event_ts": "2026-05-09T00:00:02+00:00",
                    "detail_json": {},
                },
                deliverable_event_args={
                    "event_id": "event-deliverable-1",
                    "draft_id": "draft-1",
                    "decision_id": "decision-1",
                    "deliverable_id": "deliverable-1",
                    "participant_id": "operator",
                    "event_kind": "deliverable_created",
                    "event_ts": "2026-05-09T00:00:03+00:00",
                    "detail_json": {},
                },
            )

        assert collab_store.get_draft(conn, "draft-1") is None
        assert collab_store.get_decision(conn, "decision-1") is None
        assert collab_store.get_deliverable(conn, "deliverable-1") is None
    finally:
        conn.close()


def test_collab_deliverable_must_match_approved_decision():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_deliverable must match approved decision",
        ):
            conn.execute(
                """INSERT INTO collab_deliverables
                      (deliverable_id, draft_id, decision_id, thread_id,
                       from_participant, from_participant_type, from_transport_type,
                       kind, payload_json, content_text, correlation_id, created_ts)
                   VALUES
                      ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
                       'dev-codex', 'agent', 'mcp', 'text',
                       '{"body":"tampered"}', 'tampered', 'corr-1',
                       '2026-05-09T00:00:03+00:00')"""
            )
    finally:
        conn.close()


def test_collab_deliverable_rejects_noncanonical_payload_serialization():
    conn = _conn()
    try:
        conn.executescript(
            """
            INSERT INTO collab_threads VALUES
                ('thread-1', '2026-05-09T00:00:00+00:00', 'coordination', 'open');
            INSERT INTO collab_drafts
                (draft_id, thread_id, from_participant, from_participant_type,
                 from_transport_type, kind, payload_json, content_text,
                 correlation_id, created_ts)
            VALUES
                ('draft-1', 'thread-1', 'dev-codex', 'agent', 'mcp', 'text',
                 '{"a":1,"b":2}', 'draft', 'corr-1', '2026-05-09T00:00:01+00:00');
            INSERT INTO collab_operator_decisions
                (decision_id, draft_id, operator_participant, decision_type,
                 final_payload_json, final_content_text, reason, decision_ts)
            VALUES
                ('decision-1', 'draft-1', 'operator', 'approve',
                 '{"b":2,"a":1}', 'draft', NULL, '2026-05-09T00:00:02+00:00');
            INSERT INTO collab_events
                (event_id, draft_id, decision_id, deliverable_id, participant_id,
                 event_kind, event_ts, detail_json)
            VALUES
                ('event-decision-1', 'draft-1', 'decision-1', NULL, 'operator',
                 'operator_approve', '2026-05-09T00:00:02+00:00', '{}');
        """
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_deliverable must match approved decision",
        ):
            conn.execute(
                """INSERT INTO collab_deliverables
                      (deliverable_id, draft_id, decision_id, thread_id,
                       from_participant, from_participant_type, from_transport_type,
                       kind, payload_json, content_text, correlation_id, created_ts)
                   VALUES
                      ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
                       'dev-codex', 'agent', 'mcp', 'text',
                       '{"a":1,"b":2}', 'draft', 'corr-1',
                       '2026-05-09T00:00:03+00:00')"""
            )
    finally:
        conn.close()


def test_collab_receipts_require_decision_recipient():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        _insert_matching_deliverable(conn)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_receipt requires decision recipient",
        ):
            conn.execute(
                """INSERT INTO collab_receipts
                      (deliverable_id, recipient_participant, recipient_type,
                       recipient_transport, recipient_order)
                   VALUES ('deliverable-1', 'flow-claude', 'agent', 'mcp', 1)"""
            )
    finally:
        conn.close()


def test_collab_receipts_require_decision_recipient_order():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        _insert_matching_deliverable(conn)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_receipt requires decision recipient",
        ):
            conn.execute(
                """INSERT INTO collab_receipts
                      (deliverable_id, recipient_participant, recipient_type,
                       recipient_transport, recipient_order)
                   VALUES ('deliverable-1', 'prod-codex', 'agent', 'mcp', 1)"""
            )
    finally:
        conn.close()


def test_collab_receipts_require_deliverable_created_event():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        conn.execute(
            """INSERT INTO collab_deliverables
                  (deliverable_id, draft_id, decision_id, thread_id,
                   from_participant, from_participant_type, from_transport_type,
                   kind, payload_json, content_text, correlation_id, created_ts)
               VALUES
                  ('deliverable-1', 'draft-1', 'decision-1', 'thread-1',
                   'dev-codex', 'agent', 'mcp', 'text', '{"body":"draft"}', 'draft',
                   'corr-1', '2026-05-09T00:00:03+00:00')"""
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_receipt requires decision recipient",
        ):
            conn.execute(
                """INSERT INTO collab_receipts
                      (deliverable_id, recipient_participant, recipient_type,
                       recipient_transport, recipient_order)
                   VALUES ('deliverable-1', 'prod-codex', 'agent', 'mcp', 0)"""
            )
    finally:
        conn.close()


def test_collab_receipt_recipient_authority_is_immutable_after_insert():
    conn = _conn()
    try:
        _seed_approved_decision(conn)
        _insert_matching_deliverable(conn)
        conn.execute(
            """INSERT INTO collab_receipts
                  (deliverable_id, recipient_participant, recipient_type,
                   recipient_transport, recipient_order)
               VALUES ('deliverable-1', 'prod-codex', 'agent', 'mcp', 0)"""
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_receipts recipient authority is immutable",
        ):
            conn.execute(
                """UPDATE collab_receipts
                      SET recipient_participant = 'flow-claude'
                    WHERE deliverable_id = 'deliverable-1'
                      AND recipient_participant = 'prod-codex'"""
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="collab_receipts recipient authority is immutable",
        ):
            conn.execute(
                """UPDATE collab_receipts
                      SET recipient_order = 1
                    WHERE deliverable_id = 'deliverable-1'
                      AND recipient_participant = 'prod-codex'"""
            )
    finally:
        conn.close()


def test_collaboration_sql_mutations_are_owned_by_store_seam():
    source_root = Path(__file__).resolve().parents[1] / "src" / "agent_broker"
    store_path = source_root / "collaboration" / "collab_store.py"
    mutating_collab_sql = re.compile(
        r"\b(?:"
        r"INSERT\s+INTO|"
        r"UPDATE|"
        r"DELETE\s+FROM|"
        r"REPLACE\s+INTO|"
        r"CREATE\s+(?:TABLE|TRIGGER|INDEX)(?:\s+IF\s+NOT\s+EXISTS)?"
        r")\s+[`\"']?(?:IDX_)?COLLAB_"
    )
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        if path == store_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            normalized = " ".join(node.value.upper().split())
            if mutating_collab_sql.search(normalized):
                offenders.append(f"{path.relative_to(source_root)}:{node.lineno}")

    assert offenders == []
