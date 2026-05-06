"""
--------------------------------------------------------------------------------
FILE:        test_mcp.py
PATH:        ~/projects/agent-broker/tests/test_mcp.py
DESCRIPTION: MCP direct-call and Streamable HTTP regression tests.

CHANGELOG:
2026-05-06 14:02      Codex      [Feature] Cover probe exclusion in MCP review request roster.
2026-05-06 13:43      Codex      [Feature] Add round-7 MCP concurrency immutability and round-8 probe-auth coverage.
2026-05-06 11:21      Codex      [Feature] Add peer MCP negative envelopes and explicit participant identity claim coverage.
2026-05-06 09:55      Codex      [Feature] Add Phase 6 peer MCP auth and round-trip coverage.
2026-05-06 09:11      Codex      [Feature] Add round-3 MCP HTTP authority-shape and behavioral auth-pinning tests.
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------

MCP tool tests — direct module-level invocation (not JSON-RPC framing)."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _reload_full_stack, _write_full_agents, _write_permanently_hidden_threads

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse_payload(response) -> dict:
    """Extract the first JSON-RPC payload from a Streamable HTTP SSE response."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"no SSE data line in response: {response.text!r}")


def _mcp_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1:8420",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _initialize_mcp_session(client: TestClient) -> dict[str, str]:
    headers = _mcp_headers()
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "agent-broker-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("mcp-session-id")
    headers["mcp-session-id"] = response.headers["mcp-session-id"]
    return headers


def _call_mcp_tool(
    client: TestClient,
    headers: dict[str, str],
    *,
    request_id: int,
    name: str,
    arguments: dict,
):
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _mcp_http_stack(tmp_path, monkeypatch):
    agents_path = tmp_path / "agents.json"
    _write_full_agents(agents_path)
    permanently_hidden_threads_path = tmp_path / "operator_permanently_hidden_threads.json"
    _write_permanently_hidden_threads(permanently_hidden_threads_path)
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_AGENT_SECRETS_PATH", str(tmp_path / "agent-secrets.json"))
    monkeypatch.setenv(
        "OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH", str(permanently_hidden_threads_path)
    )
    monkeypatch.setenv("OPERATOR_PARTICIPANT_ID", "operator")
    monkeypatch.setenv("DELPHI_DB_PATH", str(tmp_path / "broker.sqlite"))
    monkeypatch.setenv("DELPHI_HOST", "127.0.0.1")
    monkeypatch.setenv("DELPHI_PORT", "8420")
    monkeypatch.setenv("DELPHI_MCP_HOST_REGISTRY", "127.0.0.1:*,localhost:*,testserver")
    monkeypatch.setenv(
        "DELPHI_MCP_ORIGIN_REGISTRY",
        "http://127.0.0.1:8420,http://localhost:8420",
    )
    monkeypatch.setenv("DELPHI_WEB_SECURE", "false")
    monkeypatch.setenv("DELPHI_NUDGE_SWEEP_ENABLED", "false")
    monkeypatch.setenv("DELPHI_MCP_SESSION_MANAGER_ENABLED", "true")
    monkeypatch.setenv("DELPHI_OPERATOR_TOKEN", "test-operator-token")
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "exec-codex")
    return _reload_full_stack()


def _write_peer_agents(agents_path):
    agents_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "pi-claude",
                        "host": "pi",
                        "role": "arbitrator",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "p" * 64,
                    },
                    {
                        "agent_id": "pi-codex",
                        "host": "pi",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "q" * 64,
                    },
                    {
                        "agent_id": "exec-codex",
                        "host": "exec",
                        "role": "executor",
                        "participant_type": "agent",
                        "transport_type": "mcp",
                        "is_probe": False,
                        "secret": "f" * 64,
                    },
                    {
                        "agent_id": "operator",
                        "host": "pi",
                        "role": "operator",
                        "participant_type": "operator",
                        "transport_type": "http",
                        "is_probe": False,
                        "secret": "g" * 64,
                    },
                    {
                        "agent_id": "pi-claude-probe",
                        "host": "pi",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "http",
                        "is_probe": True,
                        "secret": "h" * 64,
                    },
                    {
                        "agent_id": "pi-codex-probe",
                        "host": "pi",
                        "role": "worker",
                        "participant_type": "agent",
                        "transport_type": "http",
                        "is_probe": True,
                        "secret": "i" * 64,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def _mcp_peer_http_stack(tmp_path, monkeypatch):
    agents_path = tmp_path / "agents.json"
    _write_peer_agents(agents_path)
    permanently_hidden_threads_path = tmp_path / "operator_permanently_hidden_threads.json"
    _write_permanently_hidden_threads(permanently_hidden_threads_path)
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_AGENT_SECRETS_PATH", str(tmp_path / "agent-secrets.json"))
    monkeypatch.setenv(
        "OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH", str(permanently_hidden_threads_path)
    )
    monkeypatch.setenv("OPERATOR_PARTICIPANT_ID", "operator")
    monkeypatch.setenv("DELPHI_DB_PATH", str(tmp_path / "broker.sqlite"))
    monkeypatch.setenv("DELPHI_HOST", "127.0.0.1")
    monkeypatch.setenv("DELPHI_PORT", "8420")
    monkeypatch.setenv("DELPHI_MCP_HOST_REGISTRY", "127.0.0.1:*,localhost:*,testserver")
    monkeypatch.setenv(
        "DELPHI_MCP_ORIGIN_REGISTRY",
        "http://127.0.0.1:8420,http://localhost:8420",
    )
    monkeypatch.setenv("DELPHI_WEB_SECURE", "false")
    monkeypatch.setenv("DELPHI_NUDGE_SWEEP_ENABLED", "false")
    monkeypatch.setenv("DELPHI_MCP_SESSION_MANAGER_ENABLED", "true")
    monkeypatch.setenv("DELPHI_OPERATOR_TOKEN", "test-operator-token")
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "pi-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "exec-codex")
    return _reload_full_stack()


def _drive_round_1_iteration(database, workflow, conn, iteration_id, *, output, sa):
    """Skip the nudge then route through the workflow to advance state."""
    iteration = database.get_iteration(conn, iteration_id)
    if iteration["status"] == "awaiting_nudge":
        database.skip_nudge(conn, iteration_id)
    return workflow.on_destination_response(
        conn,
        iteration_id,
        output=output,
        self_assessment=sa,
        rationale="r",
    )


def _converge_host(database, workflow, conn, session_id, host):
    rounds = database.list_rounds_for_session(conn, session_id)
    rnd = next(r for r in rounds if r["round_type"] == "same_host_pair" and r["host"] == host)
    first = database.latest_iteration_for_round(conn, rnd["id"])
    _drive_round_1_iteration(
        database,
        workflow,
        conn,
        first["id"],
        output="draft text body",
        sa="more_work_needed",
    )
    second = database.latest_iteration_for_round(conn, rnd["id"])
    _drive_round_1_iteration(
        database,
        workflow,
        conn,
        second["id"],
        output="draft text body",
        sa="converged",
    )


def _walk_to_round_3(api_harness, conn):
    workflow = api_harness.workflow
    database = api_harness.database
    session = workflow.start_session(conn, problem_text="x", nudge_window_secs=0)
    _converge_host(database, workflow, conn, session["id"], "prod")
    _converge_host(database, workflow, conn, session["id"], "dev")
    rounds = database.list_rounds_for_session(conn, session["id"])
    arb = next(r for r in rounds if r["round_type"] == "cross_host_arbitration")
    arb_iter = database.latest_iteration_for_round(conn, arb["id"])
    _drive_round_1_iteration(
        database,
        workflow,
        conn,
        arb_iter["id"],
        output="FLOW_SYNTHESIS_v1",
        sa="converged",
    )
    return session


def _approve_round_3(api_harness, conn, session_id):
    """All four reviewers approve so the executor round spawns."""
    workflow = api_harness.workflow
    database = api_harness.database
    rounds = database.list_rounds_for_session(conn, session_id)
    review = next(r for r in rounds if r["round_type"] == "multi_agent_review")
    for reviewer in ("prod-claude", "prod-codex", "dev-claude", "dev-codex"):
        workflow.on_review_emitted(
            conn,
            round_id=review["id"],
            reviewer_agent=reviewer,
            decision="approve",
            comments=None,
            rationale="lgtm",
        )


# ---------------------------------------------------------------------------
# 0. FastMCP registration + HTTP transport auth pins
# ---------------------------------------------------------------------------


def test_registered_mcp_tools_are_hmac_gated(api_harness):
    tools = api_harness.mcp_server.mcp._tool_manager._tools
    assert tools
    assert not any("spike" in name or name.startswith("push_") for name in tools)
    for name, tool in tools.items():
        required = set(tool.parameters.get("required", []))
        assert {"agent_id", "client_ts", "signature"}.issubset(required), name
        assert "_verify(" in inspect.getsource(tool.fn), name


def test_mcp_http_path_rejects_foreign_host(tmp_path, monkeypatch):
    _, _, _, _, main = _mcp_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        response = client.post(
            "/mcp",
            headers={
                **_mcp_headers(),
                "Host": "evil.example.com:8420",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-broker-test", "version": "1"},
                },
            },
        )
    assert response.status_code == 421


def test_mcp_http_path_rejects_foreign_origin(tmp_path, monkeypatch):
    _, _, _, _, main = _mcp_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        response = client.post(
            "/mcp",
            headers={
                **_mcp_headers(),
                "Origin": "http://example.test:8420",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-broker-test", "version": "1"},
                },
            },
        )
    assert response.status_code == 403
    assert response.text == "Invalid Origin header"
    assert "origin_rejected" not in response.text


def test_mcp_http_path_accepts_registered_host_without_origin(tmp_path, monkeypatch):
    _, _, _, _, main = _mcp_http_stack(tmp_path, monkeypatch)
    headers = _mcp_headers()
    headers.pop("Origin")
    with TestClient(main.app) as client:
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-broker-test", "version": "1"},
                },
            },
        )
    assert response.status_code == 200, response.text
    assert _sse_payload(response)["result"]["protocolVersion"] == "2025-06-18"


def test_mcp_http_v3_tool_auth_failure_and_success(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_http_stack(tmp_path, monkeypatch)
    conn = database.get_connection(config.DB_PATH)
    try:
        database.init_db(conn)
    finally:
        conn.close()

    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        ts = _now()
        bad_response = _call_mcp_tool(
            client,
            headers,
            request_id=2,
            name="delphi_v3_get_pending_task",
            arguments={
                "agent_id": "flow-claude",
                "client_ts": ts,
                "signature": "bad",
            },
        )
        bad_payload = _sse_payload(bad_response)
        bad_text = bad_payload["result"]["content"][0]["text"]
        assert json.loads(bad_text)["error"] == "auth_failed"

        good_sig = database.compute_signature(
            "e" * 64,
            "v3_get_pending_task",
            "flow-claude",
            ts,
        )
        good_response = _call_mcp_tool(
            client,
            headers,
            request_id=3,
            name="delphi_v3_get_pending_task",
            arguments={
                "agent_id": "flow-claude",
                "client_ts": ts,
                "signature": good_sig,
            },
        )
        good_payload = _sse_payload(good_response)
        good_text = good_payload["result"]["content"][0]["text"]
        assert json.loads(good_text) == {"tasks": []}


def test_mcp_delphi_poll_inbox_excludes_probe_review_requests(tmp_path, monkeypatch):
    config, database, workflow, _, main = _mcp_http_stack(tmp_path, monkeypatch)
    conn = database.get_connection(config.DB_PATH)
    try:
        database.init_db(conn)
        session = _walk_to_round_3(
            type("Harness", (), {"workflow": workflow, "database": database}),
            conn,
        )
        review_rounds = [
            row
            for row in database.list_rounds_for_session(conn, session["id"])
            if row["round_type"] == "multi_agent_review" and row["status"] == "in_progress"
        ]
        assert review_rounds
    finally:
        conn.close()

    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        poll_ts = _now()
        poll_sig = database.compute_signature("j" * 64, "poll_inbox", "prod-probe", poll_ts)
        response = _call_mcp_tool(
            client,
            headers,
            request_id=25,
            name="delphi_poll_inbox",
            arguments={
                "agent_id": "prod-probe",
                "client_ts": poll_ts,
                "signature": poll_sig,
            },
        )
    payload = json.loads(_sse_payload(response)["result"]["content"][0]["text"])
    assert "error" not in payload
    assert payload["reviews_pending"] == []


def test_all_registered_mcp_tools_reject_bogus_hmac_behaviorally(tmp_path, monkeypatch):
    config, database, _, mcp_server, main = _mcp_http_stack(tmp_path, monkeypatch)
    conn = database.get_connection(config.DB_PATH)
    try:
        database.init_db(conn)
    finally:
        conn.close()

    ts = _now()
    tool_arguments = {
        "delphi_poll_inbox": {
            "agent_id": "flow-claude",
            "client_ts": ts,
            "signature": "bogus",
        },
        "delphi_emit_response": {
            "agent_id": "flow-claude",
            "request_id": "00000000-0000-0000-0000-000000000001",
            "client_ts": ts,
            "signature": "bogus",
            "output": "x",
            "self_assessment": "converged",
            "rationale": "x",
        },
        "delphi_emit_review": {
            "agent_id": "flow-claude",
            "request_id": "00000000-0000-0000-0000-000000000002",
            "client_ts": ts,
            "signature": "bogus",
            "decision": "approve",
            "comments": "x",
            "rationale": "x",
        },
        "delphi_executor_emit": {
            "agent_id": "exec-codex",
            "request_id": "00000000-0000-0000-0000-000000000003",
            "client_ts": ts,
            "signature": "bogus",
            "success": True,
            "output": "x",
            "error": "",
        },
        "delphi_v3_get_pending_task": {
            "agent_id": "flow-claude",
            "client_ts": ts,
            "signature": "bogus",
        },
        "delphi_v3_dispatch": {
            "agent_id": "flow-claude",
            "client_ts": ts,
            "signature": "bogus",
            "task_id": "00000000-0000-0000-0000-000000000004",
            "worker_id": "prod-claude",
            "subtask_text": "x",
            "subtask_json": {"scope": "test"},
        },
        "delphi_v3_collect_outputs": {
            "agent_id": "flow-claude",
            "client_ts": ts,
            "signature": "bogus",
            "task_id": "00000000-0000-0000-0000-000000000005",
        },
        "delphi_v3_aggregate": {
            "agent_id": "flow-claude",
            "client_ts": ts,
            "signature": "bogus",
            "task_id": "00000000-0000-0000-0000-000000000006",
            "synthesis_text": "x",
            "decision": "done",
            "refine_directive": "",
            "synthesis_json": {"scope": "test"},
        },
        "delphi_v3_poll_dispatches": {
            "agent_id": "prod-claude",
            "client_ts": ts,
            "signature": "bogus",
        },
        "delphi_v3_emit_output": {
            "agent_id": "prod-claude",
            "client_ts": ts,
            "signature": "bogus",
            "dispatch_id": "00000000-0000-0000-0000-000000000007",
            "output_text": "x",
            "output_json": {"scope": "test"},
        },
        "peer_send": {
            "agent_id": "prod-claude",
            "participant_type": "agent",
            "transport_type": "mcp",
            "client_ts": ts,
            "signature": "bogus",
            "to_participants": ["prod-codex"],
            "message_kind": "text",
            "payload_json": {"body": "x"},
            "content_text": "x",
            "correlation_id": "corr-peer-bogus",
            "parent_message_id": None,
            "thread_id": None,
            "subject": "test",
        },
        "peer_poll": {
            "agent_id": "prod-codex",
            "participant_type": "agent",
            "transport_type": "mcp",
            "client_ts": ts,
            "signature": "bogus",
            "limit": 10,
        },
        "peer_ack": {
            "agent_id": "prod-codex",
            "participant_type": "agent",
            "transport_type": "mcp",
            "client_ts": ts,
            "signature": "bogus",
            "message_id": "00000000-0000-0000-0000-000000000008",
        },
        "peer_get_thread": {
            "agent_id": "prod-codex",
            "participant_type": "agent",
            "transport_type": "mcp",
            "client_ts": ts,
            "signature": "bogus",
            "thread_id": "00000000-0000-0000-0000-000000000009",
        },
    }
    registered = set(mcp_server.mcp._tool_manager._tools)
    assert registered == set(tool_arguments)

    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        for index, (tool_name, arguments) in enumerate(tool_arguments.items(), start=10):
            response = _call_mcp_tool(
                client,
                headers,
                request_id=index,
                name=tool_name,
                arguments=arguments,
            )
            assert response.status_code == 200, tool_name
            payload = _sse_payload(response)
            text = payload["result"]["content"][0]["text"]
            assert json.loads(text)["error"] == "auth_failed", tool_name


def test_mcp_peer_send_poll_ack_round_trip(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    conn = database.get_connection(config.DB_PATH)
    try:
        database.init_db(conn)
    finally:
        conn.close()

    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        send_ts = _now()
        send_sig = database.compute_signature(
            "p" * 64,
            "peer_send",
            "pi-claude",
            "agent",
            "mcp",
            send_ts,
            "corr-mcp-peer",
        )
        send_response = _call_mcp_tool(
            client,
            headers,
            request_id=40,
            name="peer_send",
            arguments={
                "agent_id": "pi-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": send_ts,
                "signature": send_sig,
                "to_participants": ["pi-codex"],
                "message_kind": "text",
                "payload_json": {"body": "hello codex"},
                "content_text": "hello codex",
                "correlation_id": "corr-mcp-peer",
                "parent_message_id": None,
                "thread_id": None,
                "subject": "mcp peer test",
            },
        )
        send_payload = json.loads(_sse_payload(send_response)["result"]["content"][0]["text"])
        message_id = send_payload["message"]["message_id"]
        thread_id = send_payload["message"]["thread_id"]
        assert send_payload["error"] is None

        poll_ts = _now()
        poll_sig = database.compute_signature(
            "q" * 64,
            "peer_poll",
            "pi-codex",
            "agent",
            "mcp",
            poll_ts,
            "10",
        )
        poll_response = _call_mcp_tool(
            client,
            headers,
            request_id=41,
            name="peer_poll",
            arguments={
                "agent_id": "pi-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": poll_ts,
                "signature": poll_sig,
                "limit": 10,
            },
        )
        poll_payload = json.loads(_sse_payload(poll_response)["result"]["content"][0]["text"])
        assert [message["message_id"] for message in poll_payload["messages"]] == [message_id]

        ack_ts = _now()
        ack_sig = database.compute_signature(
            "q" * 64,
            "peer_ack",
            "pi-codex",
            "agent",
            "mcp",
            ack_ts,
            message_id,
        )
        ack_response = _call_mcp_tool(
            client,
            headers,
            request_id=42,
            name="peer_ack",
            arguments={
                "agent_id": "pi-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": ack_ts,
                "signature": ack_sig,
                "message_id": message_id,
            },
        )
        ack_payload = json.loads(_sse_payload(ack_response)["result"]["content"][0]["text"])
        assert ack_payload["message_id"] == message_id
        assert ack_payload["error"] is None
        assert ack_payload["acked_ts"] is not None

        thread_ts = _now()
        thread_sig = database.compute_signature(
            "q" * 64,
            "peer_get_thread",
            "pi-codex",
            "agent",
            "mcp",
            thread_ts,
            thread_id,
        )
        thread_response = _call_mcp_tool(
            client,
            headers,
            request_id=43,
            name="peer_get_thread",
            arguments={
                "agent_id": "pi-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": thread_ts,
                "signature": thread_sig,
                "thread_id": thread_id,
            },
        )
        thread_payload = json.loads(_sse_payload(thread_response)["result"]["content"][0]["text"])
        assert [message["message_id"] for message in thread_payload["messages"]] == [message_id]


def test_mcp_peer_self_message_and_idempotent_ack_envelopes(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    conn = database.get_connection(config.DB_PATH)
    try:
        database.init_db(conn)
    finally:
        conn.close()

    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        self_ts = _now()
        self_sig = database.compute_signature(
            "p" * 64,
            "peer_send",
            "pi-claude",
            "agent",
            "mcp",
            self_ts,
            "corr-self",
        )
        self_response = _call_mcp_tool(
            client,
            headers,
            request_id=50,
            name="peer_send",
            arguments={
                "agent_id": "pi-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": self_ts,
                "signature": self_sig,
                "to_participants": ["pi-claude"],
                "message_kind": "text",
                "payload_json": {"body": "self"},
                "content_text": "self",
                "correlation_id": "corr-self",
                "parent_message_id": None,
                "thread_id": None,
                "subject": "self",
            },
        )
        self_payload = json.loads(_sse_payload(self_response)["result"]["content"][0]["text"])
        assert self_payload["error"]["error"] == "forbidden_recipient"

        send_ts = _now()
        send_sig = database.compute_signature(
            "p" * 64,
            "peer_send",
            "pi-claude",
            "agent",
            "mcp",
            send_ts,
            "corr-idem",
        )
        send_response = _call_mcp_tool(
            client,
            headers,
            request_id=51,
            name="peer_send",
            arguments={
                "agent_id": "pi-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": send_ts,
                "signature": send_sig,
                "to_participants": ["pi-codex"],
                "message_kind": "text",
                "payload_json": {"body": "idem"},
                "content_text": "idem",
                "correlation_id": "corr-idem",
                "parent_message_id": None,
                "thread_id": None,
                "subject": "idem",
            },
        )
        message_id = json.loads(_sse_payload(send_response)["result"]["content"][0]["text"])[
            "message"
        ]["message_id"]
        for request_id in (52, 53):
            ack_ts = _now()
            ack_sig = database.compute_signature(
                "q" * 64,
                "peer_ack",
                "pi-codex",
                "agent",
                "mcp",
                ack_ts,
                message_id,
            )
            ack_response = _call_mcp_tool(
                client,
                headers,
                request_id=request_id,
                name="peer_ack",
                arguments={
                    "agent_id": "pi-codex",
                    "participant_type": "agent",
                    "transport_type": "mcp",
                    "client_ts": ack_ts,
                    "signature": ack_sig,
                    "message_id": message_id,
                },
            )
            ack_payload = json.loads(_sse_payload(ack_response)["result"]["content"][0]["text"])
        assert ack_payload["error"]["error"] == "ack_idempotent"


def test_mcp_peer_ack_foreign_message_returns_forbidden_recipient(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        send_ts = _now()
        send_sig = database.compute_signature(
            "p" * 64,
            "peer_send",
            "pi-claude",
            "agent",
            "mcp",
            send_ts,
            "corr-foreign-ack",
        )
        send_response = _call_mcp_tool(
            client,
            headers,
            request_id=60,
            name="peer_send",
            arguments={
                "agent_id": "pi-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": send_ts,
                "signature": send_sig,
                "to_participants": ["pi-codex"],
                "message_kind": "text",
                "payload_json": {"body": "foreign"},
                "content_text": "foreign",
                "correlation_id": "corr-foreign-ack",
                "parent_message_id": None,
                "thread_id": None,
                "subject": "foreign",
            },
        )
        message_id = json.loads(_sse_payload(send_response)["result"]["content"][0]["text"])[
            "message"
        ]["message_id"]
        ack_ts = _now()
        ack_sig = database.compute_signature(
            "f" * 64,
            "peer_ack",
            "exec-codex",
            "agent",
            "mcp",
            ack_ts,
            message_id,
        )
        ack_response = _call_mcp_tool(
            client,
            headers,
            request_id=61,
            name="peer_ack",
            arguments={
                "agent_id": "exec-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": ack_ts,
                "signature": ack_sig,
                "message_id": message_id,
            },
        )
    payload = json.loads(_sse_payload(ack_response)["result"]["content"][0]["text"])
    assert payload["error"]["error"] == "forbidden_recipient"


def test_mcp_peer_ack_unknown_message_returns_message_not_found(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        ack_ts = _now()
        ack_sig = database.compute_signature(
            "q" * 64,
            "peer_ack",
            "pi-codex",
            "agent",
            "mcp",
            ack_ts,
            "missing-message",
        )
        ack_response = _call_mcp_tool(
            client,
            headers,
            request_id=62,
            name="peer_ack",
            arguments={
                "agent_id": "pi-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": ack_ts,
                "signature": ack_sig,
                "message_id": "missing-message",
            },
        )
    payload = json.loads(_sse_payload(ack_response)["result"]["content"][0]["text"])
    assert payload["error"]["error"] == "message_not_found"


def test_mcp_peer_send_participant_type_mismatch_returns_distinct_error(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        send_ts = _now()
        send_sig = database.compute_signature(
            "p" * 64,
            "peer_send",
            "pi-claude",
            "operator",
            "mcp",
            send_ts,
            "corr-type-mismatch",
        )
        send_response = _call_mcp_tool(
            client,
            headers,
            request_id=63,
            name="peer_send",
            arguments={
                "agent_id": "pi-claude",
                "participant_type": "operator",
                "transport_type": "mcp",
                "client_ts": send_ts,
                "signature": send_sig,
                "to_participants": ["pi-codex"],
                "message_kind": "text",
                "payload_json": {"body": "mismatch"},
                "content_text": "mismatch",
                "correlation_id": "corr-type-mismatch",
                "parent_message_id": None,
                "thread_id": None,
                "subject": "mismatch",
            },
        )
    payload = json.loads(_sse_payload(send_response)["result"]["content"][0]["text"])
    assert payload["error"]["error"] == "participant_type_mismatch"


def test_mcp_peer_send_blank_correlation_id_returns_invalid_payload(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        send_ts = _now()
        send_sig = database.compute_signature(
            "p" * 64,
            "peer_send",
            "pi-claude",
            "agent",
            "mcp",
            send_ts,
            "",
        )
        send_response = _call_mcp_tool(
            client,
            headers,
            request_id=64,
            name="peer_send",
            arguments={
                "agent_id": "pi-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "client_ts": send_ts,
                "signature": send_sig,
                "to_participants": ["pi-codex"],
                "message_kind": "text",
                "payload_json": {"body": "blank corr"},
                "content_text": "blank corr",
                "correlation_id": "",
                "parent_message_id": None,
                "thread_id": None,
                "subject": "blank corr",
            },
        )
    payload = json.loads(_sse_payload(send_response)["result"]["content"][0]["text"])
    assert payload["error"]["error"] == "invalid_payload"


def test_mcp_probe_identity_authenticates_but_receives_no_work(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)
        poll_ts = _now()
        poll_sig = database.compute_signature(
            "i" * 64,
            "peer_poll",
            "pi-codex-probe",
            "agent",
            "http",
            poll_ts,
            "10",
        )
        poll_response = _call_mcp_tool(
            client,
            headers,
            request_id=65,
            name="peer_poll",
            arguments={
                "agent_id": "pi-codex-probe",
                "participant_type": "agent",
                "transport_type": "http",
                "client_ts": poll_ts,
                "signature": poll_sig,
                "limit": 10,
            },
        )
    payload = json.loads(_sse_payload(poll_response)["result"]["content"][0]["text"])
    assert payload["error"] is None
    assert payload["messages"] == []


def test_mcp_peer_concurrent_send_keeps_message_immutability_trigger(tmp_path, monkeypatch):
    config, database, _, _, main = _mcp_peer_http_stack(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        headers = _initialize_mcp_session(client)

        async def send_one(index: int):
            send_ts = _now()
            correlation_id = f"corr-concurrent-{index}"
            send_sig = database.compute_signature(
                "p" * 64,
                "peer_send",
                "pi-claude",
                "agent",
                "mcp",
                send_ts,
                correlation_id,
            )
            return await asyncio.to_thread(
                _call_mcp_tool,
                client,
                headers,
                request_id=1000 + index,
                name="peer_send",
                arguments={
                    "agent_id": "pi-claude",
                    "participant_type": "agent",
                    "transport_type": "mcp",
                    "client_ts": send_ts,
                    "signature": send_sig,
                    "to_participants": ["pi-codex"],
                    "message_kind": "text",
                    "payload_json": {"body": f"concurrent {index}"},
                    "content_text": f"concurrent {index}",
                    "correlation_id": correlation_id,
                    "parent_message_id": None,
                    "thread_id": None,
                    "subject": "concurrent storm",
                },
            )

        async def storm():
            return await asyncio.gather(*(send_one(index) for index in range(8)))

        responses = asyncio.run(storm())
        message_ids = [
            json.loads(_sse_payload(response)["result"]["content"][0]["text"])["message"][
                "message_id"
            ]
            for response in responses
        ]

    raw = sqlite3.connect(config.DB_PATH)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="peer_messages are immutable"):
            raw.execute(
                "UPDATE peer_messages SET content_text = ? WHERE message_id = ?",
                ("mutated", message_ids[0]),
            )
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# 1. delphi_poll_inbox: valid signature returns iterations
# ---------------------------------------------------------------------------


def test_poll_inbox_valid_signature_returns_iterations(api_harness, agent_secrets, signed_request):
    workflow = api_harness.workflow
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = workflow.start_session(conn, problem_text="hello", nudge_window_secs=0)
        # Skip the nudge so prod-claude has work in awaiting_destination.
        rounds = database.list_rounds_for_session(conn, session["id"])
        prod_round = next(
            r for r in rounds if r["round_type"] == "same_host_pair" and r["host"] == "prod"
        )
        first = database.latest_iteration_for_round(conn, prod_round["id"])
        database.skip_nudge(conn, first["id"])
    finally:
        conn.close()

    ts = _now()
    fields = ("poll_inbox", "prod-claude", ts)
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_poll_inbox(agent_id="prod-claude", client_ts=ts, signature=sig)
    assert "error" not in result, result
    assert isinstance(result["iterations"], list)
    assert len(result["iterations"]) == 1
    item = result["iterations"][0]
    assert item["round_type"] == "same_host_pair"
    assert "request_id" in item
    assert "input_text" in item


# ---------------------------------------------------------------------------
# 2. delphi_poll_inbox: bad signature returns auth_failed
# ---------------------------------------------------------------------------


def test_poll_inbox_bad_signature_returns_auth_failed(api_harness):
    mcp_server = api_harness.mcp_server
    ts = _now()
    result = mcp_server.delphi_poll_inbox(
        agent_id="prod-claude", client_ts=ts, signature="deadbeef"
    )
    assert result["error"] == "auth_failed"


# ---------------------------------------------------------------------------
# 3. delphi_emit_response valid -> workflow advances
# ---------------------------------------------------------------------------


def test_emit_response_advances_workflow(api_harness, agent_secrets, signed_request):
    workflow = api_harness.workflow
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = workflow.start_session(conn, problem_text="hello", nudge_window_secs=0)
        rounds = database.list_rounds_for_session(conn, session["id"])
        prod_round = next(
            r for r in rounds if r["round_type"] == "same_host_pair" and r["host"] == "prod"
        )
        first = database.latest_iteration_for_round(conn, prod_round["id"])
        database.skip_nudge(conn, first["id"])
    finally:
        conn.close()

    ts = _now()
    fields = database.build_emit_response_signature_fields(
        agent_id="prod-claude",
        iteration_id=first["id"],
        timestamp=ts,
        output="draft v1",
        self_assessment="more_work_needed",
        rationale="r",
    )
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_emit_response(
        agent_id="prod-claude",
        request_id=first["id"],
        client_ts=ts,
        signature=sig,
        output="draft v1",
        self_assessment="more_work_needed",
        rationale="r",
    )
    assert "error" not in result, result
    assert result["ok"] is True
    # A second iteration should have been spawned with prod-codex destination.
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        iters = database.list_iterations_for_round(conn, prod_round["id"])
        assert len(iters) == 2
        assert iters[-1]["destination_agent"] == "prod-codex"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. delphi_emit_review valid -> review recorded
# ---------------------------------------------------------------------------


def test_emit_review_records_review(api_harness, agent_secrets, signed_request):
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = _walk_to_round_3(api_harness, conn)
        rounds = database.list_rounds_for_session(conn, session["id"])
        review_round = next(r for r in rounds if r["round_type"] == "multi_agent_review")
        round_id = review_round["id"]
    finally:
        conn.close()

    ts = _now()
    fields = database.build_emit_review_signature_fields(
        agent_id="prod-claude",
        round_id=round_id,
        timestamp=ts,
        decision="approve",
        comments=None,
        rationale="lgtm",
    )
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_emit_review(
        agent_id="prod-claude",
        request_id=round_id,
        client_ts=ts,
        signature=sig,
        decision="APPROVE",
        rationale="lgtm",
    )
    assert "error" not in result, result
    assert result["ok"] is True
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        reviews = database.list_reviews_for_round(conn, round_id)
        assert any(r["reviewer_agent"] == "prod-claude" for r in reviews)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. delphi_executor_emit valid -> session completes
# ---------------------------------------------------------------------------


def test_executor_emit_completes_session(api_harness, agent_secrets, signed_request):
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = _walk_to_round_3(api_harness, conn)
        _approve_round_3(api_harness, conn, session["id"])
        rounds = database.list_rounds_for_session(conn, session["id"])
        exec_round = next(r for r in rounds if r["round_type"] == "execute")
        exec_iter = database.latest_iteration_for_round(conn, exec_round["id"])
        # Skip the executor's nudge so destination_agent is in flight.
        if exec_iter["status"] == "awaiting_nudge":
            database.skip_nudge(conn, exec_iter["id"])
        exec_iter_id = exec_iter["id"]
    finally:
        conn.close()

    ts = _now()
    fields = database.build_executor_emit_signature_fields(
        agent_id="exec-codex",
        iteration_id=exec_iter_id,
        timestamp=ts,
        success=True,
        output="executed",
        error=None,
    )
    sig = signed_request("exec-codex", fields)
    result = mcp_server.delphi_executor_emit(
        agent_id="exec-codex",
        request_id=exec_iter_id,
        client_ts=ts,
        signature=sig,
        success=True,
        output="executed",
    )
    assert "error" not in result, result
    assert result["ok"] is True
    assert result["session_status"] == "complete"


# ---------------------------------------------------------------------------
# 6. Stale timestamp -> auth_failed
# ---------------------------------------------------------------------------


def test_stale_timestamp_returns_auth_failed(api_harness, agent_secrets, signed_request):
    mcp_server = api_harness.mcp_server
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    fields = ("poll_inbox", "prod-claude", stale)
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_poll_inbox(agent_id="prod-claude", client_ts=stale, signature=sig)
    assert result["error"] == "auth_failed"


# ---------------------------------------------------------------------------
# 7. Wrong agent emitting response -> auth_failed
# ---------------------------------------------------------------------------


def test_wrong_agent_emit_response_returns_auth_failed(api_harness, agent_secrets, signed_request):
    workflow = api_harness.workflow
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = workflow.start_session(conn, problem_text="hello", nudge_window_secs=0)
        rounds = database.list_rounds_for_session(conn, session["id"])
        prod_round = next(
            r for r in rounds if r["round_type"] == "same_host_pair" and r["host"] == "prod"
        )
        first = database.latest_iteration_for_round(conn, prod_round["id"])
        database.skip_nudge(conn, first["id"])
        first_id = first["id"]
    finally:
        conn.close()

    # prod-codex tries to emit on prod-claude's iteration. Sign with prod-codex's
    # own secret so signature passes, but the destination check fails.
    ts = _now()
    fields = database.build_emit_response_signature_fields(
        agent_id="prod-codex",
        iteration_id=first_id,
        timestamp=ts,
        output="x",
        self_assessment="converged",
        rationale="r",
    )
    sig = signed_request("prod-codex", fields)
    result = mcp_server.delphi_emit_response(
        agent_id="prod-codex",
        request_id=first_id,
        client_ts=ts,
        signature=sig,
        output="x",
        self_assessment="converged",
        rationale="r",
    )
    assert result["error"] == "auth_failed"


# ---------------------------------------------------------------------------
# 8. Non-executor agent calling delphi_executor_emit -> auth_failed
# ---------------------------------------------------------------------------


def test_non_executor_calling_executor_emit_returns_auth_failed(
    api_harness, agent_secrets, signed_request
):
    mcp_server = api_harness.mcp_server
    ts = _now()
    fields = api_harness.database.build_executor_emit_signature_fields(
        agent_id="prod-claude",
        iteration_id="anything",
        timestamp=ts,
        success=True,
        output="x",
        error=None,
    )
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_executor_emit(
        agent_id="prod-claude",
        request_id="anything",
        client_ts=ts,
        signature=sig,
        success=True,
        output="x",
    )
    assert result["error"] == "auth_failed"
