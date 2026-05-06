"""
--------------------------------------------------------------------------------
FILE:        test_database.py
PATH:        ~/projects/agent-broker/tests/test_database.py
DESCRIPTION: Data-layer regression tests for schema, DAO behavior, and HMAC builders.

CHANGELOG:
2026-05-06 14:21      Codex      [Fix] Cover config-seeded agent upsert behavior for probe flags.
2026-05-06 13:57      Codex      [Fix] Assert operator registry seeding and explicit non-probe legacy agents.
2026-05-06 09:11      Codex      [Lint] Add host-standard file header after Black reformatted this test module during Phase 3 cleanup.
--------------------------------------------------------------------------------

Tests for the v2 data layer: schema, DAO, and HMAC builders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_create_session_returns_full_row_with_defaults(conn, data_layer):
    db = data_layer.database
    session = db.create_session(conn, problem_text="ship the broker")
    assert session["problem_text"] == "ship the broker"
    assert session["status"] == "drafting"
    assert session["nudge_window_secs"] == 60
    assert session["finalized_prompt"] is None
    assert session["created_at"] == session["updated_at"]
    # round-trip via get_session
    fetched = db.get_session(conn, session["id"])
    assert fetched == session


def test_get_session_returns_none_for_unknown_id(conn, data_layer):
    assert data_layer.database.get_session(conn, "no-such-id") is None


def test_list_sessions_filters_by_status_and_orders_desc(conn, data_layer):
    db = data_layer.database
    a = db.create_session(conn, problem_text="alpha")
    b = db.create_session(conn, problem_text="beta")
    db.update_session_status(conn, a["id"], "round_1")
    rows = db.list_sessions(conn)
    assert {r["id"] for r in rows} == {a["id"], b["id"]}
    drafting = db.list_sessions(conn, status="drafting")
    assert [r["id"] for r in drafting] == [b["id"]]
    round_1 = db.list_sessions(conn, status="round_1")
    assert [r["id"] for r in round_1] == [a["id"]]


def test_update_session_status_walks_lifecycle(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="lifecycle")
    for status in ("round_1", "round_2", "round_3", "executing", "complete"):
        updated = db.update_session_status(conn, s["id"], status)
        assert updated["status"] == status


def test_update_session_status_rejects_invalid_status(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    with pytest.raises(ValueError):
        db.update_session_status(conn, s["id"], "bogus")


def test_set_finalized_prompt_writes_text(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    db.set_finalized_prompt(conn, s["id"], "the final synthesized prompt")
    refreshed = db.get_session(conn, s["id"])
    assert refreshed["finalized_prompt"] == "the final synthesized prompt"


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------


def test_create_round_starts_pending(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    r = db.create_round(
        conn,
        session_id=s["id"],
        round_num=1,
        round_type="same_host_pair",
        host="prod",
    )
    assert r["status"] == "pending"
    assert r["round_num"] == 1
    assert r["host"] == "prod"
    assert r["ended_at"] is None


def test_update_round_status_terminal_sets_ended_at(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    r = db.create_round(
        conn,
        session_id=s["id"],
        round_num=1,
        round_type="same_host_pair",
        host="prod",
    )
    in_progress = db.update_round_status(conn, r["id"], "in_progress")
    assert in_progress["ended_at"] is None
    converged = db.update_round_status(conn, r["id"], "converged", outcome_text="final draft")
    assert converged["status"] == "converged"
    assert converged["ended_at"] is not None
    assert converged["outcome_text"] == "final draft"


def test_list_rounds_for_session_orders_by_round_then_host(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    r1_dev = db.create_round(
        conn,
        session_id=s["id"],
        round_num=1,
        round_type="same_host_pair",
        host="dev",
    )
    r1_prod = db.create_round(
        conn,
        session_id=s["id"],
        round_num=1,
        round_type="same_host_pair",
        host="prod",
    )
    r2 = db.create_round(
        conn,
        session_id=s["id"],
        round_num=2,
        round_type="cross_host_arbitration",
    )
    rows = db.list_rounds_for_session(conn, s["id"])
    assert [r["id"] for r in rows] == [r1_dev["id"], r1_prod["id"], r2["id"]]


def test_current_round_for_session_picks_highest_open_round(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    r1 = db.create_round(
        conn, session_id=s["id"], round_num=1, round_type="same_host_pair", host="prod"
    )
    db.update_round_status(conn, r1["id"], "converged")
    r2 = db.create_round(conn, session_id=s["id"], round_num=2, round_type="cross_host_arbitration")
    current = db.current_round_for_session(conn, s["id"])
    assert current["id"] == r2["id"]


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------


def _make_round(db, conn):
    s = db.create_session(conn, problem_text="x", nudge_window_secs=30)
    r = db.create_round(
        conn,
        session_id=s["id"],
        round_num=1,
        round_type="same_host_pair",
        host="prod",
    )
    return s, r


def test_iteration_lifecycle_apply_nudge_then_response(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    it = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="please refine this prompt",
        nudge_window_secs=60,
    )
    assert it["status"] == "awaiting_nudge"
    assert it["destination_received_at"] is None

    nudged = db.apply_nudge(conn, it["id"], "focus on clarity")
    assert nudged["status"] == "awaiting_destination"
    assert nudged["nudge_text"] == "focus on clarity"
    assert nudged["destination_received_at"] is not None

    done = db.record_destination_response(
        conn,
        it["id"],
        output="refined prompt v1",
        self_assessment="more_work_needed",
        rationale="needs another pass for tone",
    )
    assert done["status"] == "complete"
    assert done["destination_output"] == "refined prompt v1"
    assert done["destination_self_assess"] == "more_work_needed"
    assert done["destination_emitted_at"] is not None


def test_iteration_skip_nudge_leaves_nudge_text_null(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    it = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="seed",
        nudge_window_secs=60,
    )
    skipped = db.skip_nudge(conn, it["id"])
    assert skipped["status"] == "awaiting_destination"
    assert skipped["nudge_text"] is None
    assert skipped["destination_received_at"] is not None


def test_apply_nudge_rejects_blank_text(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    it = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="seed",
        nudge_window_secs=60,
    )
    with pytest.raises(ValueError):
        db.apply_nudge(conn, it["id"], "")


def test_record_destination_response_requires_awaiting_destination(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    it = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="seed",
        nudge_window_secs=60,
    )
    # still in awaiting_nudge -- emit must fail
    with pytest.raises(ValueError):
        db.record_destination_response(
            conn,
            it["id"],
            output="x",
            self_assessment="converged",
            rationale="x",
        )


def test_off_script_terminates_iteration(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    it = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent="prod-claude",
        destination_agent="prod-codex",
        source_output="here is a draft",
        nudge_window_secs=60,
    )
    db.skip_nudge(conn, it["id"])
    off = db.mark_iteration_off_script(conn, it["id"], "missing JSON fields")
    assert off["status"] == "off_script"
    assert off["destination_rationale"] == "missing JSON fields"
    # cannot move it again once terminal
    with pytest.raises(ValueError):
        db.mark_iteration_off_script(conn, it["id"], "again")


def test_find_pending_iterations_returns_only_expired_windows(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    fresh = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="seed-1",
        nudge_window_secs=300,  # five minutes — well into the future
    )
    expired = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=2,
        source_agent="prod-claude",
        destination_agent="prod-codex",
        source_output="seed-2",
        nudge_window_secs=0,  # already closed
    )
    # ensure window-closed timestamp is comfortably in the past
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn.execute(
        "UPDATE iterations SET nudge_window_closes_at = ? WHERE id = ?",
        (past, expired["id"]),
    )
    conn.commit()

    pending = db.find_pending_iterations(conn)
    ids = {row["id"] for row in pending}
    assert expired["id"] in ids
    assert fresh["id"] not in ids


def test_find_inbox_for_agent_returns_only_destination_matches(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    addressed = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="hi",
        nudge_window_secs=0,
    )
    other = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=2,
        source_agent="prod-claude",
        destination_agent="prod-codex",
        source_output="hi-2",
        nudge_window_secs=0,
    )
    db.skip_nudge(conn, addressed["id"])
    db.skip_nudge(conn, other["id"])
    inbox = db.find_inbox_for_agent(conn, "prod-claude")
    assert [row["id"] for row in inbox] == [addressed["id"]]


def test_latest_iteration_for_round_returns_highest_iter_num(conn, data_layer):
    db = data_layer.database
    _, r = _make_round(db, conn)
    db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=1,
        source_agent=None,
        destination_agent="prod-claude",
        source_output="a",
        nudge_window_secs=0,
    )
    second = db.create_iteration(
        conn,
        round_id=r["id"],
        iter_num=2,
        source_agent="prod-claude",
        destination_agent="prod-codex",
        source_output="b",
        nudge_window_secs=0,
    )
    latest = db.latest_iteration_for_round(conn, r["id"])
    assert latest["id"] == second["id"]


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def test_create_review_and_find_pending_reviewers(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    r3 = db.create_round(conn, session_id=s["id"], round_num=3, round_type="multi_agent_review")
    expected = ["prod-claude", "prod-codex", "dev-claude", "dev-codex"]
    assert db.find_pending_reviewers_for_round(conn, r3["id"], expected) == expected

    db.create_review(
        conn,
        round_id=r3["id"],
        reviewer_agent="prod-claude",
        decision="approve",
        rationale="lgtm",
    )
    db.create_review(
        conn,
        round_id=r3["id"],
        reviewer_agent="dev-codex",
        decision="reject",
        comments="fix tone",
        rationale="too terse",
    )
    pending = db.find_pending_reviewers_for_round(conn, r3["id"], expected)
    assert pending == ["prod-codex", "dev-claude"]
    listed = db.list_reviews_for_round(conn, r3["id"])
    assert {row["reviewer_agent"] for row in listed} == {"prod-claude", "dev-codex"}


def test_create_review_rejects_invalid_decision(conn, data_layer):
    db = data_layer.database
    s = db.create_session(conn, problem_text="x")
    r3 = db.create_round(conn, session_id=s["id"], round_num=3, round_type="multi_agent_review")
    with pytest.raises(ValueError):
        db.create_review(
            conn,
            round_id=r3["id"],
            reviewer_agent="prod-claude",
            decision="maybe",
        )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_seed_agents_present_after_init(conn, data_layer):
    db = data_layer.database
    rows = db.list_agents(conn)
    ids = {row["agent_id"] for row in rows}
    assert ids == {
        "prod-claude",
        "prod-codex",
        "dev-claude",
        "dev-codex",
        "flow-claude",
        "operator",
    }
    flow = db.get_agent(conn, "flow-claude")
    assert flow["role"] == "arbitrator"
    assert flow["host"] == "flow"
    assert flow["is_probe"] == 0


def test_verify_agent_and_touch_updates_last_seen(conn, data_layer):
    db = data_layer.database
    assert db.verify_agent(conn, "prod-claude") is True
    assert db.verify_agent(conn, "ghost") is False
    before = db.get_agent(conn, "prod-claude")["last_seen"]
    db.touch_agent(conn, "prod-claude")
    after = db.get_agent(conn, "prod-claude")["last_seen"]
    assert after >= before


def test_seed_agents_upsert_config_owned_fields(conn, data_layer):
    db = data_layer.database
    conn.execute(
        """UPDATE agents
              SET host = ?, role = ?, is_probe = ?
            WHERE agent_id = ?""",
        ("drift", "worker", 1, "flow-claude"),
    )
    conn.commit()

    db.init_db(conn)
    flow = db.get_agent(conn, "flow-claude")

    assert flow["host"] == "flow"
    assert flow["role"] == "arbitrator"
    assert flow["is_probe"] == 0


# ---------------------------------------------------------------------------
# HMAC signature builders
# ---------------------------------------------------------------------------


def test_signature_builders_prefix_action_and_are_distinct(data_layer):
    db = data_layer.database
    secret = "topsecret"
    ts = "2026-04-25T12:00:00+00:00"

    sig_create = db.compute_signature(
        secret,
        *db.build_create_session_signature_fields(
            sender="prod-claude", timestamp=ts, problem_text="hello"
        ),
    )
    sig_nudge = db.compute_signature(
        secret,
        *db.build_nudge_signature_fields(
            sender="prod-claude",
            iteration_id="iter-1",
            timestamp=ts,
            action="submit",
            nudge_text="be concise",
        ),
    )
    sig_emit = db.compute_signature(
        secret,
        *db.build_emit_response_signature_fields(
            agent_id="prod-claude",
            iteration_id="iter-1",
            timestamp=ts,
            output="answer",
            self_assessment="converged",
            rationale="r",
        ),
    )
    sig_review = db.compute_signature(
        secret,
        *db.build_emit_review_signature_fields(
            agent_id="prod-claude",
            round_id="round-1",
            timestamp=ts,
            decision="approve",
            comments=None,
            rationale="lgtm",
        ),
    )
    sig_executor = db.compute_signature(
        secret,
        *db.build_executor_emit_signature_fields(
            agent_id="dev-codex",
            iteration_id="iter-1",
            timestamp=ts,
            success=True,
            output="ran",
            error=None,
        ),
    )
    sig_abort = db.compute_signature(
        secret,
        *db.build_abort_signature_fields(sender="prod-claude", session_id="s-1", timestamp=ts),
    )

    sigs = [sig_create, sig_nudge, sig_emit, sig_review, sig_executor, sig_abort]
    assert len(set(sigs)) == len(sigs), "every action must produce a distinct signature"


def test_signature_is_stable_for_identical_inputs(data_layer):
    db = data_layer.database
    secret = "topsecret"
    ts = "2026-04-25T12:00:00+00:00"
    fields = db.build_emit_response_signature_fields(
        agent_id="prod-claude",
        iteration_id="iter-1",
        timestamp=ts,
        output="answer",
        self_assessment="converged",
        rationale=None,
    )
    assert db.compute_signature(secret, *fields) == db.compute_signature(secret, *fields)


def test_signature_changes_when_self_assessment_changes(data_layer):
    db = data_layer.database
    secret = "topsecret"
    ts = "2026-04-25T12:00:00+00:00"
    base_kwargs = dict(
        agent_id="prod-claude",
        iteration_id="iter-1",
        timestamp=ts,
        output="answer",
        rationale="r",
    )
    sig_a = db.compute_signature(
        secret,
        *db.build_emit_response_signature_fields(self_assessment="converged", **base_kwargs),
    )
    sig_b = db.compute_signature(
        secret,
        *db.build_emit_response_signature_fields(self_assessment="more_work_needed", **base_kwargs),
    )
    assert sig_a != sig_b


def test_check_timestamp_freshness_rejects_old_timestamps(data_layer):
    db = data_layer.database
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert db.check_timestamp_freshness(fresh) is True
    assert db.check_timestamp_freshness(stale) is False
    assert db.check_timestamp_freshness("not-a-date") is False


def test_executor_signature_distinguishes_success_flag(data_layer):
    db = data_layer.database
    secret = "topsecret"
    ts = "2026-04-25T12:00:00+00:00"
    sig_ok = db.compute_signature(
        secret,
        *db.build_executor_emit_signature_fields(
            agent_id="dev-codex",
            iteration_id="iter-1",
            timestamp=ts,
            success=True,
            output="ok",
            error=None,
        ),
    )
    sig_fail = db.compute_signature(
        secret,
        *db.build_executor_emit_signature_fields(
            agent_id="dev-codex",
            iteration_id="iter-1",
            timestamp=ts,
            success=False,
            output="ok",
            error="boom",
        ),
    )
    assert sig_ok != sig_fail
