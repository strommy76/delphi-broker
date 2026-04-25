"""MCP tool tests — direct module-level invocation (not JSON-RPC framing)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    rnd = next(
        r for r in rounds
        if r["round_type"] == "same_host_pair" and r["host"] == host
    )
    first = database.latest_iteration_for_round(conn, rnd["id"])
    _drive_round_1_iteration(
        database, workflow, conn, first["id"],
        output="draft text body", sa="more_work_needed",
    )
    second = database.latest_iteration_for_round(conn, rnd["id"])
    _drive_round_1_iteration(
        database, workflow, conn, second["id"],
        output="draft text body", sa="converged",
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
        database, workflow, conn, arb_iter["id"],
        output="FLOW_SYNTHESIS_v1", sa="converged",
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
# 1. delphi_poll_inbox: valid signature returns iterations
# ---------------------------------------------------------------------------


def test_poll_inbox_valid_signature_returns_iterations(
    api_harness, agent_secrets, signed_request
):
    workflow = api_harness.workflow
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = workflow.start_session(
            conn, problem_text="hello", nudge_window_secs=0
        )
        # Skip the nudge so prod-claude has work in awaiting_destination.
        rounds = database.list_rounds_for_session(conn, session["id"])
        prod_round = next(
            r for r in rounds
            if r["round_type"] == "same_host_pair" and r["host"] == "prod"
        )
        first = database.latest_iteration_for_round(conn, prod_round["id"])
        database.skip_nudge(conn, first["id"])
    finally:
        conn.close()

    ts = _now()
    fields = ("poll_inbox", "prod-claude", ts)
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_poll_inbox(
        agent_id="prod-claude", client_ts=ts, signature=sig
    )
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


def test_emit_response_advances_workflow(
    api_harness, agent_secrets, signed_request
):
    workflow = api_harness.workflow
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = workflow.start_session(
            conn, problem_text="hello", nudge_window_secs=0
        )
        rounds = database.list_rounds_for_session(conn, session["id"])
        prod_round = next(
            r for r in rounds
            if r["round_type"] == "same_host_pair" and r["host"] == "prod"
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


def test_emit_review_records_review(
    api_harness, agent_secrets, signed_request
):
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = _walk_to_round_3(api_harness, conn)
        rounds = database.list_rounds_for_session(conn, session["id"])
        review_round = next(
            r for r in rounds if r["round_type"] == "multi_agent_review"
        )
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


def test_executor_emit_completes_session(
    api_harness, agent_secrets, signed_request
):
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


def test_stale_timestamp_returns_auth_failed(
    api_harness, agent_secrets, signed_request
):
    mcp_server = api_harness.mcp_server
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    fields = ("poll_inbox", "prod-claude", stale)
    sig = signed_request("prod-claude", fields)
    result = mcp_server.delphi_poll_inbox(
        agent_id="prod-claude", client_ts=stale, signature=sig
    )
    assert result["error"] == "auth_failed"


# ---------------------------------------------------------------------------
# 7. Wrong agent emitting response -> auth_failed
# ---------------------------------------------------------------------------


def test_wrong_agent_emit_response_returns_auth_failed(
    api_harness, agent_secrets, signed_request
):
    workflow = api_harness.workflow
    database = api_harness.database
    mcp_server = api_harness.mcp_server
    conn = database.get_connection(api_harness.config.DB_PATH)
    try:
        session = workflow.start_session(
            conn, problem_text="hello", nudge_window_secs=0
        )
        rounds = database.list_rounds_for_session(conn, session["id"])
        prod_round = next(
            r for r in rounds
            if r["round_type"] == "same_host_pair" and r["host"] == "prod"
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
