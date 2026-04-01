from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timezone


def _fresh_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _web_auth_headers(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _create_message(
    broker_stack,
    *,
    sender: str = "worker-a",
    recipients: str = "worker-b",
    status: str = "PENDING",
) -> dict:
    conn = broker_stack.database.get_connection(broker_stack.config.DB_PATH)
    try:
        return broker_stack.database.submit_message(
            conn,
            sender=sender,
            channel="review",
            subject="subject",
            body="body",
            recipients=recipients,
            status=status,
        )
    finally:
        conn.close()


def test_submit_signature_covers_priority_parent_id_and_metadata(broker_stack):
    payload = {
        "sender": "worker-a",
        "channel": "review",
        "subject": "signed",
        "body": "body",
        "recipients": "worker-b",
        "priority": "urgent",
        "parent_id": "parent-1",
        "metadata": {"z": 1, "a": 2},
        "timestamp": _fresh_timestamp(),
    }

    payload["signature"] = broker_stack.sign(
        "worker-a",
        "submit",
        payload["sender"],
        payload["channel"],
        payload["timestamp"],
        payload["subject"],
        payload["body"],
        payload["recipients"],
    )
    bad_response = broker_stack.client.post("/api/v1/messages", json=payload)
    assert bad_response.status_code == 403

    payload["signature"] = broker_stack.sign(
        "worker-a",
        *broker_stack.database.build_submit_signature_fields(
            sender=payload["sender"],
            channel=payload["channel"],
            timestamp=payload["timestamp"],
            subject=payload["subject"],
            body=payload["body"],
            recipients=payload["recipients"],
            priority=payload["priority"],
            parent_id=payload["parent_id"],
            metadata=payload["metadata"],
        ),
    )
    good_response = broker_stack.client.post("/api/v1/messages", json=payload)
    assert good_response.status_code == 200


def test_approve_and_reject_signatures_cover_note_and_reason(broker_stack):
    pending = _create_message(broker_stack)
    approve_payload = {
        "agent_id": "orch",
        "note": "ship it",
        "timestamp": _fresh_timestamp(),
    }
    approve_payload["signature"] = broker_stack.sign(
        "orch",
        "approve",
        approve_payload["agent_id"],
        pending["message_id"],
        approve_payload["timestamp"],
    )
    bad_approve = broker_stack.client.post(
        f"/api/v1/messages/{pending['message_id']}/approve", json=approve_payload
    )
    assert bad_approve.status_code == 403

    approve_payload["signature"] = broker_stack.sign(
        "orch",
        *broker_stack.database.build_approve_signature_fields(
            agent_id=approve_payload["agent_id"],
            message_id=pending["message_id"],
            timestamp=approve_payload["timestamp"],
            note=approve_payload["note"],
        ),
    )
    good_approve = broker_stack.client.post(
        f"/api/v1/messages/{pending['message_id']}/approve", json=approve_payload
    )
    assert good_approve.status_code == 200

    pending = _create_message(broker_stack)
    reject_payload = {
        "agent_id": "orch",
        "reason": "needs work",
        "timestamp": _fresh_timestamp(),
    }
    reject_payload["signature"] = broker_stack.sign(
        "orch",
        "reject",
        reject_payload["agent_id"],
        pending["message_id"],
        reject_payload["timestamp"],
    )
    bad_reject = broker_stack.client.post(
        f"/api/v1/messages/{pending['message_id']}/reject", json=reject_payload
    )
    assert bad_reject.status_code == 403

    reject_payload["signature"] = broker_stack.sign(
        "orch",
        *broker_stack.database.build_reject_signature_fields(
            agent_id=reject_payload["agent_id"],
            message_id=pending["message_id"],
            timestamp=reject_payload["timestamp"],
            reason=reject_payload["reason"],
        ),
    )
    good_reject = broker_stack.client.post(
        f"/api/v1/messages/{pending['message_id']}/reject", json=reject_payload
    )
    assert good_reject.status_code == 200


def test_broadcast_signature_covers_priority_and_auto_approve(broker_stack):
    payload = {
        "sender": "orch",
        "channel": "ops",
        "subject": "directive",
        "body": "body",
        "priority": "urgent",
        "auto_approve": False,
        "timestamp": _fresh_timestamp(),
    }
    payload["signature"] = broker_stack.sign(
        "orch",
        "broadcast",
        payload["sender"],
        payload["channel"],
        payload["timestamp"],
        payload["subject"],
        payload["body"],
    )
    bad_response = broker_stack.client.post("/api/v1/messages/broadcast", json=payload)
    assert bad_response.status_code == 403

    payload["signature"] = broker_stack.sign(
        "orch",
        *broker_stack.database.build_broadcast_signature_fields(
            sender=payload["sender"],
            channel=payload["channel"],
            timestamp=payload["timestamp"],
            subject=payload["subject"],
            body=payload["body"],
            priority=payload["priority"],
            auto_approve=payload["auto_approve"],
        ),
    )
    good_response = broker_stack.client.post("/api/v1/messages/broadcast", json=payload)
    assert good_response.status_code == 200


def test_ack_requires_recipient_membership_and_is_idempotent(broker_stack):
    approved = _create_message(broker_stack, recipients="worker-b", status="APPROVED")

    wrong_ack = {
        "agent_id": "worker-a",
        "timestamp": _fresh_timestamp(),
    }
    wrong_ack["signature"] = broker_stack.sign(
        "worker-a",
        *broker_stack.database.build_ack_signature_fields(
            agent_id=wrong_ack["agent_id"],
            message_id=approved["message_id"],
            timestamp=wrong_ack["timestamp"],
        ),
    )
    wrong_response = broker_stack.client.post(
        f"/api/v1/messages/{approved['message_id']}/ack", json=wrong_ack
    )
    assert wrong_response.status_code == 403

    ack_payload = {
        "agent_id": "worker-b",
        "timestamp": _fresh_timestamp(),
    }
    ack_payload["signature"] = broker_stack.sign(
        "worker-b",
        *broker_stack.database.build_ack_signature_fields(
            agent_id=ack_payload["agent_id"],
            message_id=approved["message_id"],
            timestamp=ack_payload["timestamp"],
        ),
    )
    first_ack = broker_stack.client.post(
        f"/api/v1/messages/{approved['message_id']}/ack", json=ack_payload
    )
    assert first_ack.status_code == 200
    first_acked_at = first_ack.json()["acked_at"]

    ack_payload["timestamp"] = _fresh_timestamp()
    ack_payload["signature"] = broker_stack.sign(
        "worker-b",
        *broker_stack.database.build_ack_signature_fields(
            agent_id=ack_payload["agent_id"],
            message_id=approved["message_id"],
            timestamp=ack_payload["timestamp"],
        ),
    )
    second_ack = broker_stack.client.post(
        f"/api/v1/messages/{approved['message_id']}/ack", json=ack_payload
    )
    assert second_ack.status_code == 200
    assert second_ack.json()["acked_at"] == first_acked_at

    receipts = broker_stack.client.get(f"/api/v1/messages/{approved['message_id']}/receipts")
    assert receipts.status_code == 200
    assert receipts.json() == [
        {
            "message_id": approved["message_id"],
            "recipient": "worker-b",
            "acked_at": first_acked_at,
        }
    ]


def test_web_routes_require_basic_auth(broker_stack):
    unauthorized = broker_stack.client.get("/web/")
    assert unauthorized.status_code == 401

    authorized = broker_stack.client.get(
        "/web/",
        headers=_web_auth_headers(broker_stack.config.WEB_UI_AGENT_ID, "web-secret"),
    )
    assert authorized.status_code == 200


def test_existing_database_is_migrated_for_new_columns(broker_stack, tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy_db)
    try:
        conn.executescript("""
            CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY,
                host TEXT NOT NULL,
                roles TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipients TEXT NOT NULL DEFAULT '*',
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'PENDING',
                submitted_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                decision_note TEXT DEFAULT '',
                parent_id TEXT,
                metadata TEXT DEFAULT '{}'
            );
            """)
        conn.commit()
    finally:
        conn.close()

    conn = broker_stack.database.get_connection(legacy_db)
    try:
        columns = {
            row["name"]: row["type"]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert "signature" in columns
        assert "client_ts" in columns
        receipts_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'message_receipts'"
        ).fetchone()
        assert receipts_table is not None
    finally:
        conn.close()
