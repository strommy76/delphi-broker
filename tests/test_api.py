"""Operator REST surface tests (FastAPI TestClient)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# 1. Create session: valid token -> 201 with session_id
# ---------------------------------------------------------------------------


def test_create_session_returns_201_with_id(client, api_harness):
    resp = client.post(
        "/api/v1/session",
        json={"problem_text": "ship the broker"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "session_id" in body
    assert body["status"] == "round_1"
    # Confirm the row really landed in the DB.
    conn = api_harness.database.get_connection(api_harness.config.DB_PATH)
    try:
        session = api_harness.database.get_session(conn, body["session_id"])
        assert session is not None
        assert session["problem_text"] == "ship the broker"
    finally:
        conn.close()


def test_create_session_rejects_bad_token(client, api_harness):
    bad = client.post(
        "/api/v1/session",
        headers={"X-Operator-Token": "wrong"},
        json={"problem_text": "x"},
    )
    assert bad.status_code == 403


def test_create_session_validates_payload(client):
    resp = client.post("/api/v1/session", json={"problem_text": ""})
    assert resp.status_code == 422  # pydantic validation


# ---------------------------------------------------------------------------
# 2-3. GET session (full dict) and 404 path
# ---------------------------------------------------------------------------


def test_get_session_returns_full_dict(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.get(f"/api/v1/session/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == session_id
    assert "rounds" in body
    assert "iterations_by_round" in body
    assert "reviews_by_round" in body
    # Two same_host_pair rounds should exist (prod + dev).
    same_host = [r for r in body["rounds"] if r["round_type"] == "same_host_pair"]
    assert len(same_host) == 2


def test_get_session_unknown_returns_404(client):
    # Use a valid uuid that doesn't exist.
    resp = client.get("/api/v1/session/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_get_session_invalid_uuid_returns_400(client):
    resp = client.get("/api/v1/session/not-a-uuid")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 4-5. Pending: 204 when none, transition when present
# ---------------------------------------------------------------------------


def test_pending_returns_transition_when_awaiting_nudge(client, api_harness):
    create = client.post(
        "/api/v1/session",
        json={"problem_text": "x", "nudge_window_secs": 300},
    )
    session_id = create.json()["session_id"]
    resp = client.get(f"/api/v1/session/{session_id}/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["transition"] is not None
    assert body["transition"]["status"] == "awaiting_nudge"


def test_pending_returns_204_when_no_open_transition(client, api_harness):
    create = client.post(
        "/api/v1/session",
        json={"problem_text": "x", "nudge_window_secs": 0},
    )
    session_id = create.json()["session_id"]
    # Force every awaiting_nudge window into the past so none are 'open'.
    conn = api_harness.database.get_connection(api_harness.config.DB_PATH)
    try:
        past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        conn.execute(
            """UPDATE iterations SET nudge_window_closes_at = ?
                 WHERE id IN (
                   SELECT i.id FROM iterations i
                   JOIN rounds r ON r.id = i.round_id
                   WHERE r.session_id = ?)""",
            (past, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    resp = client.get(f"/api/v1/session/{session_id}/pending")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# 6-7. Submit + skip nudges
# ---------------------------------------------------------------------------


def test_submit_nudge_transitions_iteration(client, api_harness):
    create = client.post(
        "/api/v1/session",
        json={"problem_text": "x", "nudge_window_secs": 300},
    )
    session_id = create.json()["session_id"]
    pending = client.get(f"/api/v1/session/{session_id}/pending").json()
    iteration_id = pending["transition"]["id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/nudge",
        json={
            "iteration_id": iteration_id,
            "action": "submit",
            "nudge_text": "be concise",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "awaiting_destination"


def test_submit_nudge_requires_text(client, api_harness):
    create = client.post(
        "/api/v1/session", json={"problem_text": "x", "nudge_window_secs": 300}
    )
    session_id = create.json()["session_id"]
    iteration_id = client.get(
        f"/api/v1/session/{session_id}/pending"
    ).json()["transition"]["id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/nudge",
        json={"iteration_id": iteration_id, "action": "submit"},
    )
    assert resp.status_code == 400


def test_skip_nudge_transitions_iteration(client, api_harness):
    create = client.post(
        "/api/v1/session", json={"problem_text": "x", "nudge_window_secs": 300}
    )
    session_id = create.json()["session_id"]
    iteration_id = client.get(
        f"/api/v1/session/{session_id}/pending"
    ).json()["transition"]["id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/nudge",
        json={"iteration_id": iteration_id, "action": "skip"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "awaiting_destination"


# ---------------------------------------------------------------------------
# 8. Abort
# ---------------------------------------------------------------------------


def test_abort_session_marks_aborted(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.post(f"/api/v1/session/{session_id}/abort")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "aborted"
    # Rounds should be aborted too.
    full = client.get(f"/api/v1/session/{session_id}").json()
    for r in full["rounds"]:
        assert r["status"] == "aborted"


# ---------------------------------------------------------------------------
# 9. Transcript
# ---------------------------------------------------------------------------


def test_transcript_returns_full_structure(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.get(f"/api/v1/session/{session_id}/transcript")
    assert resp.status_code == 200
    body = resp.json()
    assert "session" in body
    assert "rounds" in body
    assert "iterations_by_round" in body
    assert "reviews_by_round" in body


# ---------------------------------------------------------------------------
# 10. Resolve escalation: abort path
# ---------------------------------------------------------------------------


def test_resolve_escalation_with_abort_sets_aborted(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/escalation/resolve",
        json={"action": "abort"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["new_status"] == "aborted"


def test_resolve_escalation_skip_agent_records(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/escalation/resolve",
        json={"action": "skip_agent", "agent_id": "dev-codex"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_status"] == "round_1"  # unchanged from start_session


def test_resolve_escalation_unknown_action_400(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/escalation/resolve",
        json={"action": "abort"},
    )
    assert resp.status_code == 200
    # Bogus action through the API layer is rejected by Pydantic enum (422).
    bogus = client.post(
        f"/api/v1/session/{session_id}/escalation/resolve",
        json={"action": "bogus"},
    )
    assert bogus.status_code == 422


# ---------------------------------------------------------------------------
# 11. approve_execution no-op
# ---------------------------------------------------------------------------


def test_approve_execution_returns_current_status(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x"})
    session_id = create.json()["session_id"]
    resp = client.post(f"/api/v1/session/{session_id}/approve_execution")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "round_1"
