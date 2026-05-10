"""
--------------------------------------------------------------------------------
FILE:        test_api.py
PATH:        ~/projects/agent-broker/tests/test_api.py
DESCRIPTION: Operator REST API and HTTP middleware regression tests.

CHANGELOG:
2026-05-06 14:02      Codex      [Refactor] Rename operator permanently hidden thread config example assertions.
2026-05-06 13:39      Codex      [Fix] Assert operator hidden-thread config is example-backed and gitignored.
2026-05-06 09:35      Codex      [Refactor] Update config registration assertion for explicit peer participant identity fields.
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------

Operator REST surface tests (FastAPI TestClient)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_foreign_origin_rejected_by_app_middleware(client):
    resp = client.get(
        "/api/v2/agents",
        headers={
            "X-Operator-Token": "test-operator-token",
            "Origin": "http://example.test:8420",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "origin_rejected"


def test_phase_3_config_registers_pi_codex():
    root = Path(__file__).resolve().parents[1]
    agents = json.loads((root / "config" / "agents.json").read_text(encoding="utf-8"))["agents"]
    pi_codex = [agent for agent in agents if agent["agent_id"] == "pi-codex"]
    assert len(pi_codex) == 1
    assert {
        "agent_id": "pi-codex",
        "host": "pi",
        "role": "worker",
        "participant_type": "agent",
        "transport_type": "mcp",
        "is_probe": False,
        "collaboration_governed": False,
    }.items() <= pi_codex[0].items()
    assert "config/agents-secrets.json" in (root / ".gitignore").read_text(encoding="utf-8")


def test_operator_hidden_threads_config_is_gitignored_with_example():
    root = Path(__file__).resolve().parents[1]
    gitignore = (root / ".gitignore").read_text()
    example = json.loads(
        (root / "config" / "operator_permanently_hidden_threads.json.example").read_text()
    )

    assert "config/operator_permanently_hidden_threads.json" in gitignore
    assert example["thread_ids"] == []
    assert example["_meta"]["changelog"]


def test_template_brand_and_task_dispatch_titles_are_split():
    templates = Path(__file__).resolve().parents[1] / "src" / "agent_broker" / "templates"
    assert '<span class="nav-brand">Agent Broker</span>' in (templates / "base.html").read_text(
        encoding="utf-8"
    )
    assert "Tasks — Task Dispatch" in (templates / "v3_tasks_list.html").read_text(encoding="utf-8")
    assert "New task — Task Dispatch" in (templates / "v3_task_new.html").read_text(
        encoding="utf-8"
    )
    assert "{{ task.title }} — Task Dispatch" in (templates / "v3_task_view.html").read_text(
        encoding="utf-8"
    )
    assert "Sessions — Delphi" in (templates / "sessions_list.html").read_text(encoding="utf-8")


def test_v3_operator_rosters_exclude_probe_and_operator_identities(
    api_harness,
    operator_token,
):
    api_agents = api_harness.client.get("/api/v2/agents")
    assert api_agents.status_code == 200, api_agents.text
    api_ids = {agent["agent_id"] for agent in api_agents.json()["agents"]}

    api_harness.client.cookies.set("op_token", operator_token)
    web_form = api_harness.client.get("/web/v3/new")
    assert web_form.status_code == 200, web_form.text

    assert "pi-claude-probe" not in api_ids
    assert "pi-codex-probe" not in api_ids
    assert "operator" not in api_ids
    assert "pi-claude-probe" not in web_form.text
    assert "pi-codex-probe" not in web_form.text
    assert 'value="operator"' not in web_form.text


def test_v3_database_rejects_probe_and_operator_task_authority(api_harness):
    from agent_broker.v3 import database as v3db

    conn = api_harness.database.get_connection(api_harness.config.DB_PATH)
    try:
        api_harness.database.init_db(conn)
        v3db.init_v3_schema(conn)
        task_id = v3db.create_task(
            conn,
            title="probe segregation",
            problem_text="x",
            orchestrator_id="prod-claude",
        )
        with pytest.raises(ValueError, match="worker_id"):
            v3db.create_dispatch(
                conn,
                task_id=task_id,
                worker_id="pi-claude-probe",
                subtask_text="x",
                subtask_json={},
            )
        with pytest.raises(ValueError, match="orchestrator_id"):
            v3db.create_task(
                conn,
                title="operator rejected",
                problem_text="x",
                orchestrator_id="operator",
            )
        with pytest.raises(ValueError, match="orchestrator_id"):
            v3db.create_task(
                conn,
                title="probe rejected",
                problem_text="x",
                orchestrator_id="pi-claude-probe",
            )
    finally:
        conn.close()


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
    create = client.post("/api/v1/session", json={"problem_text": "x", "nudge_window_secs": 300})
    session_id = create.json()["session_id"]
    iteration_id = client.get(f"/api/v1/session/{session_id}/pending").json()["transition"]["id"]
    resp = client.post(
        f"/api/v1/session/{session_id}/nudge",
        json={"iteration_id": iteration_id, "action": "submit"},
    )
    assert resp.status_code == 400


def test_skip_nudge_transitions_iteration(client, api_harness):
    create = client.post("/api/v1/session", json={"problem_text": "x", "nudge_window_secs": 300})
    session_id = create.json()["session_id"]
    iteration_id = client.get(f"/api/v1/session/{session_id}/pending").json()["transition"]["id"]
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
