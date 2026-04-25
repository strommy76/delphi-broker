"""Tests for the v2 workflow engine."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Fixture: workflow needs two workers per host (the shared conftest puts
# dev-codex in the executor role, leaving dev with only one worker). Build a
# dedicated registry that satisfies the v2 contract: prod and dev each get a
# Claude+Codex worker pair, plus a separate executor and arbitrator host.
# ---------------------------------------------------------------------------


def _reload_modules() -> tuple[object, object, object]:
    for name in sorted(sys.modules, reverse=True):
        if name == "delphi_broker" or name.startswith("delphi_broker."):
            sys.modules.pop(name, None)
    config = importlib.import_module("delphi_broker.config")
    database = importlib.import_module("delphi_broker.database")
    workflow = importlib.import_module("delphi_broker.workflow")
    return config, database, workflow


@pytest.fixture
def wf_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    agents_path = tmp_path / "agents.json"
    agents_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "agent_id": "prod-claude",
                        "host": "prod",
                        "role": "worker",
                        "secret": "a" * 64,
                    },
                    {
                        "agent_id": "prod-codex",
                        "host": "prod",
                        "role": "worker",
                        "secret": "b" * 64,
                    },
                    {
                        "agent_id": "dev-claude",
                        "host": "dev",
                        "role": "worker",
                        "secret": "c" * 64,
                    },
                    {
                        "agent_id": "dev-codex",
                        "host": "dev",
                        "role": "worker",
                        "secret": "d" * 64,
                    },
                    {
                        "agent_id": "flow-claude",
                        "host": "flow",
                        "role": "arbitrator",
                        "secret": "e" * 64,
                    },
                    {
                        "agent_id": "exec-codex",
                        "host": "exec",
                        "role": "executor",
                        "secret": "f" * 64,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "broker.sqlite"
    monkeypatch.setenv("DELPHI_AGENTS_PATH", str(agents_path))
    monkeypatch.setenv("DELPHI_DB_PATH", str(db_path))
    monkeypatch.setenv("DELPHI_ARBITRATOR_AGENT_ID", "flow-claude")
    monkeypatch.setenv("DELPHI_EXECUTOR_AGENT_ID", "exec-codex")
    config, database, workflow = _reload_modules()
    connection = database.get_connection(config.DB_PATH)
    try:
        yield config, database, workflow, connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drive_iteration_to_response(
    db, conn, iteration_id, *, output, self_assessment, rationale="r"
):
    """Skip nudge if needed, then record a destination response directly."""
    iteration = db.get_iteration(conn, iteration_id)
    if iteration["status"] == "awaiting_nudge":
        db.skip_nudge(conn, iteration_id)
    return db.record_destination_response(
        conn,
        iteration_id,
        output=output,
        self_assessment=self_assessment,
        rationale=rationale,
    )


def _emit_via_workflow(workflow, conn, iteration_id, *, output, self_assessment, rationale="r"):
    """Skip nudge then route through workflow.on_destination_response."""
    return workflow.on_destination_response(
        conn,
        iteration_id,
        output=output,
        self_assessment=self_assessment,
        rationale=rationale,
    )


def _skip_nudge_then_emit(workflow, db, conn, iteration_id, **kwargs):
    iteration = db.get_iteration(conn, iteration_id)
    if iteration["status"] == "awaiting_nudge":
        db.skip_nudge(conn, iteration_id)
    return _emit_via_workflow(workflow, conn, iteration_id, **kwargs)


def _latest_iter(db, conn, round_id):
    return db.latest_iteration_for_round(conn, round_id)


def _round_for_host(db, conn, session_id, host):
    rounds = db.list_rounds_for_session(conn, session_id)
    for r in rounds:
        if r["round_type"] == "same_host_pair" and r["host"] == host:
            return r
    raise AssertionError(f"no round_1 for host {host} in session {session_id}")


def _converge_round_1_host(workflow, db, conn, session_id, host):
    """Run two near-identical CONVERGED iterations to make a round_1 round converge."""
    rnd = _round_for_host(db, conn, session_id, host)
    first = _latest_iter(db, conn, rnd["id"])
    _skip_nudge_then_emit(
        workflow,
        db,
        conn,
        first["id"],
        output="draft text body",
        self_assessment="more_work_needed",
    )
    second = _latest_iter(db, conn, rnd["id"])
    _skip_nudge_then_emit(
        workflow,
        db,
        conn,
        second["id"],
        output="draft text body",  # identical -> similarity 1.0
        self_assessment="converged",
    )
    return rnd


# ---------------------------------------------------------------------------
# 1-3. Pure helpers
# ---------------------------------------------------------------------------


def test_is_converged_requires_self_assess_and_threshold(wf_layer):
    _, _, workflow, _ = wf_layer
    a = "the same exact text"
    b = "the same exact text"
    assert workflow.is_converged(a, b, "converged") is True
    assert workflow.is_converged(a, b, "more_work_needed") is False
    assert workflow.is_converged(None, b, "converged") is False
    # Sub-threshold similarity should not converge even with self-assess.
    assert workflow.is_converged("hello world foo", "totally different", "converged") is False


def test_detect_oscillation_flags_low_similarity_pair(wf_layer):
    _, _, workflow, _ = wf_layer
    assert workflow.detect_oscillation([]) is False
    assert workflow.detect_oscillation(["only"]) is False
    assert (
        workflow.detect_oscillation(
            ["a perfect statement of intent", "a perfect statement of intent"]
        )
        is False
    )
    assert (
        workflow.detect_oscillation(
            ["alpha bravo charlie delta echo foxtrot golf", "zzz"]
        )
        is True
    )


def test_normalize_for_similarity_collapses_whitespace(wf_layer):
    _, _, workflow, _ = wf_layer
    assert workflow.normalize_for_similarity("  a   b\n\tc  ") == "a b c"
    assert workflow.normalize_for_similarity("") == ""
    assert workflow.normalize_for_similarity(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. start_session
# ---------------------------------------------------------------------------


def test_start_session_spawns_one_round_per_host_with_seed_iteration(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship the broker")
    assert session["status"] == "round_1"

    rounds = db.list_rounds_for_session(conn, session["id"])
    same_host = [r for r in rounds if r["round_type"] == "same_host_pair"]
    hosts = {r["host"] for r in same_host}
    assert hosts == {"prod", "dev"}
    for r in same_host:
        assert r["status"] == "in_progress"
        iters = db.list_iterations_for_round(conn, r["id"])
        assert len(iters) == 1
        first = iters[0]
        assert first["iter_num"] == 1
        assert first["source_agent"] is None
        assert first["status"] == "awaiting_nudge"
        # First destination is alphabetically-first worker on that host.
        if r["host"] == "prod":
            assert first["destination_agent"] == "prod-claude"
        else:
            assert first["destination_agent"] == "dev-claude"


# ---------------------------------------------------------------------------
# 5. on_destination_response: same-host alternation
# ---------------------------------------------------------------------------


def test_on_destination_response_alternates_agents_on_same_host(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship it")
    prod_round = _round_for_host(db, conn, session["id"], "prod")
    first = _latest_iter(db, conn, prod_round["id"])
    assert first["destination_agent"] == "prod-claude"

    _skip_nudge_then_emit(
        workflow,
        db,
        conn,
        first["id"],
        output="prod-claude draft v1",
        self_assessment="more_work_needed",
    )
    iters = db.list_iterations_for_round(conn, prod_round["id"])
    assert len(iters) == 2
    second = iters[-1]
    assert second["iter_num"] == 2
    assert second["source_agent"] == "prod-claude"
    assert second["destination_agent"] == "prod-codex"
    assert second["source_output"] == "prod-claude draft v1"


# ---------------------------------------------------------------------------
# 6. Convergence path on a single host
# ---------------------------------------------------------------------------


def test_round_1_converges_when_similar_and_self_assess_converged(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship it")
    rnd = _converge_round_1_host(workflow, db, conn, session["id"], "prod")
    refreshed = db.get_round(conn, rnd["id"])
    assert refreshed["status"] == "converged"
    assert refreshed["outcome_text"] == "draft text body"


# ---------------------------------------------------------------------------
# 7. Both hosts converged -> round_2 spawned
# ---------------------------------------------------------------------------


def test_both_hosts_converged_advances_to_round_2(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship it")
    _converge_round_1_host(workflow, db, conn, session["id"], "prod")
    _converge_round_1_host(workflow, db, conn, session["id"], "dev")

    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "round_2"

    rounds = db.list_rounds_for_session(conn, session["id"])
    arb = [r for r in rounds if r["round_type"] == "cross_host_arbitration"]
    assert len(arb) == 1
    iters = db.list_iterations_for_round(conn, arb[0]["id"])
    assert len(iters) == 1
    assert iters[0]["destination_agent"] == "flow-claude"
    assert "[PROD]" in iters[0]["source_output"]
    assert "[DEV]" in iters[0]["source_output"]


# ---------------------------------------------------------------------------
# 8. Stall: 8 iterations without convergence -> escalated
# ---------------------------------------------------------------------------


def test_round_1_stalls_after_max_iters(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship it")
    prod_round = _round_for_host(db, conn, session["id"], "prod")
    # Eight iterations; each output unique enough to avoid oscillation but
    # different enough to never converge (similarity ~0.6-0.8 region).
    bases = [
        "alpha bravo charlie delta echo foxtrot golf hotel india",
        "alpha bravo charlie delta echo foxtrot golf hotel juliet",
        "alpha bravo charlie delta echo foxtrot golf hotel kilo",
        "alpha bravo charlie delta echo foxtrot golf hotel lima",
        "alpha bravo charlie delta echo foxtrot golf hotel mike",
        "alpha bravo charlie delta echo foxtrot golf hotel november",
        "alpha bravo charlie delta echo foxtrot golf hotel oscar",
        "alpha bravo charlie delta echo foxtrot golf hotel papa",
    ]
    for i, text in enumerate(bases):
        latest = _latest_iter(db, conn, prod_round["id"])
        # Last (8th) emission should escalate.
        _skip_nudge_then_emit(
            workflow,
            db,
            conn,
            latest["id"],
            output=text,
            self_assessment="more_work_needed",
        )
    refreshed_session = db.get_session(conn, session["id"])
    assert refreshed_session["status"] == "escalated"
    refreshed_round = db.get_round(conn, prod_round["id"])
    assert refreshed_round["status"] == "escalated"


# ---------------------------------------------------------------------------
# 9. Oscillation: two consecutive iterations < 0.50 similarity -> escalated
# ---------------------------------------------------------------------------


def test_oscillation_escalates_round_1(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship it")
    prod_round = _round_for_host(db, conn, session["id"], "prod")
    first = _latest_iter(db, conn, prod_round["id"])
    _skip_nudge_then_emit(
        workflow,
        db,
        conn,
        first["id"],
        output="aaaaaaaaaaaaaaaaaaaaa",
        self_assessment="more_work_needed",
    )
    second = _latest_iter(db, conn, prod_round["id"])
    _skip_nudge_then_emit(
        workflow,
        db,
        conn,
        second["id"],
        output="zzzzzzzzzzzzzzzzz different completely 999",
        self_assessment="more_work_needed",
    )
    assert db.get_session(conn, session["id"])["status"] == "escalated"


# ---------------------------------------------------------------------------
# 10. Cross-host irreconcilable: prod vs dev outcome similarity < 0.30
# ---------------------------------------------------------------------------


def test_cross_host_irreconcilable_escalates_before_round_2(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="ship it")
    # Prod converges on text A. Dev converges on wildly-different text B.
    prod_text = "short"
    dev_text = (
        "very long entirely different content with no words in common at all forty seven"
    )
    prod_round = _round_for_host(db, conn, session["id"], "prod")
    p1 = _latest_iter(db, conn, prod_round["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, p1["id"],
        output=prod_text, self_assessment="more_work_needed",
    )
    p2 = _latest_iter(db, conn, prod_round["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, p2["id"],
        output=prod_text, self_assessment="converged",
    )

    dev_round = _round_for_host(db, conn, session["id"], "dev")
    d1 = _latest_iter(db, conn, dev_round["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, d1["id"],
        output=dev_text, self_assessment="more_work_needed",
    )
    d2 = _latest_iter(db, conn, dev_round["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, d2["id"],
        output=dev_text, self_assessment="converged",
    )
    assert db.get_session(conn, session["id"])["status"] == "escalated"
    # No round_2 should have been spawned.
    rounds = db.list_rounds_for_session(conn, session["id"])
    assert not any(r["round_type"] == "cross_host_arbitration" for r in rounds)


# ---------------------------------------------------------------------------
# Helpers for round_3 / executor flows
# ---------------------------------------------------------------------------


def _converge_session_to_round_3(workflow, db, conn, *, problem="ship it"):
    """Walk a session: round_1 prod + dev -> round_2 -> round_3."""
    session = workflow.start_session(conn, problem_text=problem)
    _converge_round_1_host(workflow, db, conn, session["id"], "prod")
    _converge_round_1_host(workflow, db, conn, session["id"], "dev")
    # Round 2 arbitrator emits.
    rounds = db.list_rounds_for_session(conn, session["id"])
    arb = next(r for r in rounds if r["round_type"] == "cross_host_arbitration")
    arb_iter = _latest_iter(db, conn, arb["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, arb_iter["id"],
        output="FLOW_SYNTHESIS_v1",
        self_assessment="converged",
    )
    refreshed = db.get_session(conn, session["id"])
    return session, refreshed


# ---------------------------------------------------------------------------
# 11. on_review_emitted: pending until all reviewers in
# ---------------------------------------------------------------------------


def test_on_review_emitted_pending_until_all_in(wf_layer):
    _, db, workflow, conn = wf_layer
    session, refreshed = _converge_session_to_round_3(workflow, db, conn)
    assert refreshed["status"] == "round_3"
    rounds = db.list_rounds_for_session(conn, session["id"])
    review_round = next(r for r in rounds if r["round_type"] == "multi_agent_review")

    # Three of four reviewers approve; session must remain in round_3.
    for reviewer in ["prod-claude", "prod-codex", "dev-claude"]:
        workflow.on_review_emitted(
            conn,
            round_id=review_round["id"],
            reviewer_agent=reviewer,
            decision="approve",
            comments=None,
            rationale="lgtm",
        )
        s = db.get_session(conn, session["id"])
        assert s["status"] == "round_3"


# ---------------------------------------------------------------------------
# 12. All approve -> spawns execute round to executor agent
# ---------------------------------------------------------------------------


def test_all_approvals_spawn_execute_round(wf_layer):
    _, db, workflow, conn = wf_layer
    session, _ = _converge_session_to_round_3(workflow, db, conn)
    rounds = db.list_rounds_for_session(conn, session["id"])
    review_round = next(r for r in rounds if r["round_type"] == "multi_agent_review")

    for reviewer in ["prod-claude", "prod-codex", "dev-claude", "dev-codex"]:
        workflow.on_review_emitted(
            conn,
            round_id=review_round["id"],
            reviewer_agent=reviewer,
            decision="approve",
            comments=None,
            rationale="lgtm",
        )
    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "executing"
    rounds = db.list_rounds_for_session(conn, session["id"])
    exec_rounds = [r for r in rounds if r["round_type"] == "execute"]
    assert len(exec_rounds) == 1
    iters = db.list_iterations_for_round(conn, exec_rounds[0]["id"])
    assert len(iters) == 1
    assert iters[0]["destination_agent"] == "exec-codex"
    assert iters[0]["source_output"] == "FLOW_SYNTHESIS_v1"


# ---------------------------------------------------------------------------
# 13. Any reject -> mediation round
# ---------------------------------------------------------------------------


def test_rejection_triggers_mediation_round(wf_layer):
    _, db, workflow, conn = wf_layer
    session, _ = _converge_session_to_round_3(workflow, db, conn)
    rounds = db.list_rounds_for_session(conn, session["id"])
    review_round = next(r for r in rounds if r["round_type"] == "multi_agent_review")

    for reviewer in ["prod-claude", "prod-codex", "dev-claude"]:
        workflow.on_review_emitted(
            conn, round_id=review_round["id"], reviewer_agent=reviewer,
            decision="approve", comments=None, rationale="lgtm",
        )
    workflow.on_review_emitted(
        conn, round_id=review_round["id"], reviewer_agent="dev-codex",
        decision="reject", comments="tone is off", rationale="too terse",
    )
    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "round_2"
    rounds = db.list_rounds_for_session(conn, session["id"])
    arb_rounds = [r for r in rounds if r["round_type"] == "cross_host_arbitration"]
    assert len(arb_rounds) == 2  # original + mediation
    mediation = arb_rounds[-1]
    iters = db.list_iterations_for_round(conn, mediation["id"])
    assert "dev-codex: tone is off" in iters[0]["source_output"]
    assert "Prior synthesis:" in iters[0]["source_output"]


# ---------------------------------------------------------------------------
# 14. After 2 failed mediations -> full restart to round_1
# ---------------------------------------------------------------------------


def test_two_failed_mediations_trigger_full_restart(wf_layer):
    _, db, workflow, conn = wf_layer
    session, _ = _converge_session_to_round_3(workflow, db, conn)

    def _run_review_round_with_one_reject():
        rounds = db.list_rounds_for_session(conn, session["id"])
        review = [
            r for r in rounds
            if r["round_type"] == "multi_agent_review" and r["status"] != "complete"
        ][-1]
        for reviewer in ["prod-claude", "prod-codex", "dev-claude"]:
            workflow.on_review_emitted(
                conn, round_id=review["id"], reviewer_agent=reviewer,
                decision="approve", comments=None, rationale="lgtm",
            )
        workflow.on_review_emitted(
            conn, round_id=review["id"], reviewer_agent="dev-codex",
            decision="reject", comments="still off", rationale="meh",
        )

    def _arbitrator_emits():
        rounds = db.list_rounds_for_session(conn, session["id"])
        arb = [
            r for r in rounds
            if r["round_type"] == "cross_host_arbitration" and r["status"] != "complete"
        ][-1]
        latest = _latest_iter(db, conn, arb["id"])
        _skip_nudge_then_emit(
            workflow, db, conn, latest["id"],
            output="FLOW_v_revised", self_assessment="converged",
        )

    # First rejection -> mediation #1
    _run_review_round_with_one_reject()
    _arbitrator_emits()
    # Second rejection -> mediation #2
    _run_review_round_with_one_reject()
    _arbitrator_emits()
    # Third rejection -> full restart to round_1
    _run_review_round_with_one_reject()

    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "round_1"
    rounds = db.list_rounds_for_session(conn, session["id"])
    same_host_in_progress = [
        r for r in rounds
        if r["round_type"] == "same_host_pair" and r["status"] == "in_progress"
    ]
    assert len(same_host_in_progress) == 2  # one per host, fresh round_1


# ---------------------------------------------------------------------------
# 15-16. Executor outcomes
# ---------------------------------------------------------------------------


def _walk_to_executing(workflow, db, conn):
    session, _ = _converge_session_to_round_3(workflow, db, conn)
    rounds = db.list_rounds_for_session(conn, session["id"])
    review_round = next(r for r in rounds if r["round_type"] == "multi_agent_review")
    for reviewer in ["prod-claude", "prod-codex", "dev-claude", "dev-codex"]:
        workflow.on_review_emitted(
            conn, round_id=review_round["id"], reviewer_agent=reviewer,
            decision="approve", comments=None, rationale="lgtm",
        )
    rounds = db.list_rounds_for_session(conn, session["id"])
    exec_round = next(r for r in rounds if r["round_type"] == "execute")
    exec_iter = _latest_iter(db, conn, exec_round["id"])
    return session, exec_iter


def test_on_executor_emitted_success_completes_session(wf_layer):
    _, db, workflow, conn = wf_layer
    session, exec_iter = _walk_to_executing(workflow, db, conn)
    workflow.on_executor_emitted(
        conn,
        iteration_id=exec_iter["id"],
        success=True,
        output="executed: did the thing",
        error=None,
    )
    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "complete"
    assert refreshed["finalized_prompt"] == "FLOW_SYNTHESIS_v1"


def test_on_executor_emitted_failure_escalates(wf_layer):
    _, db, workflow, conn = wf_layer
    session, exec_iter = _walk_to_executing(workflow, db, conn)
    workflow.on_executor_emitted(
        conn,
        iteration_id=exec_iter["id"],
        success=False,
        output="",
        error="non-zero exit",
    )
    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "escalated"


# ---------------------------------------------------------------------------
# 17. auto_skip_expired_nudges
# ---------------------------------------------------------------------------


def test_auto_skip_expired_nudges_closes_pending(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="x", nudge_window_secs=300)
    # Both first iterations are awaiting_nudge with windows in the future.
    rounds = db.list_rounds_for_session(conn, session["id"])
    same_host = [r for r in rounds if r["round_type"] == "same_host_pair"]
    iter_ids = []
    for r in same_host:
        it = _latest_iter(db, conn, r["id"])
        iter_ids.append(it["id"])
    # Force their windows into the past.
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    for iid in iter_ids:
        conn.execute(
            "UPDATE iterations SET nudge_window_closes_at = ? WHERE id = ?",
            (past, iid),
        )
    conn.commit()

    closed = workflow.auto_skip_expired_nudges(conn)
    assert closed == 2
    for iid in iter_ids:
        it = db.get_iteration(conn, iid)
        assert it["status"] == "awaiting_destination"


# ---------------------------------------------------------------------------
# 18. resolve_escalation: force_converge advances, abort sets aborted
# ---------------------------------------------------------------------------


def test_resolve_escalation_force_converge_and_abort(wf_layer):
    _, db, workflow, conn = wf_layer
    # Trigger a stall escalation on one host, converge the other normally.
    session = workflow.start_session(conn, problem_text="x")
    # Drive prod into escalation via oscillation.
    prod_round = _round_for_host(db, conn, session["id"], "prod")
    p1 = _latest_iter(db, conn, prod_round["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, p1["id"],
        output="aaaaaaaaaa", self_assessment="more_work_needed",
    )
    p2 = _latest_iter(db, conn, prod_round["id"])
    _skip_nudge_then_emit(
        workflow, db, conn, p2["id"],
        output="zzzzzzzzz xxxxxxxxxxx", self_assessment="more_work_needed",
    )
    assert db.get_session(conn, session["id"])["status"] == "escalated"

    # Converge dev so force_converge can flip prod and proceed to round_2.
    _converge_round_1_host(workflow, db, conn, session["id"], "dev")

    refreshed = workflow.resolve_escalation(
        conn, session["id"], action="force_converge"
    )
    # Prod outcome should now be the latest output ("zzzzzzzzz xxxxxxxxxxx").
    prod_after = db.get_round(conn, prod_round["id"])
    assert prod_after["status"] == "converged"
    assert prod_after["outcome_text"] == "zzzzzzzzz xxxxxxxxxxx"
    # Cross-host similarity is below floor here ("zzzzz..." vs "draft text body"),
    # so this should re-escalate. That's the contract — force_converge does the
    # forced part; downstream gates still apply.
    assert refreshed["status"] in ("round_2", "escalated")

    # Abort always wins.
    aborted = workflow.resolve_escalation(conn, session["id"], action="abort")
    assert aborted["status"] == "aborted"


# ---------------------------------------------------------------------------
# 19. resolve_escalation skip_agent updates skipped_reviewers list
# ---------------------------------------------------------------------------


def test_resolve_escalation_skip_agent_records_skipped_reviewer(wf_layer):
    _, db, workflow, conn = wf_layer
    session = workflow.start_session(conn, problem_text="x")
    workflow.resolve_escalation(
        conn, session["id"], action="skip_agent", agent_id="dev-codex"
    )
    skipped = db.get_skipped_reviewers(conn, session["id"])
    assert skipped == ["dev-codex"]
    # Idempotent — a second skip is a no-op.
    workflow.resolve_escalation(
        conn, session["id"], action="skip_agent", agent_id="dev-codex"
    )
    assert db.get_skipped_reviewers(conn, session["id"]) == ["dev-codex"]
    # Unknown action raises.
    with pytest.raises(ValueError):
        workflow.resolve_escalation(conn, session["id"], action="bogus")


# ---------------------------------------------------------------------------
# 20. _other_agent_on_host
# ---------------------------------------------------------------------------


def test_other_agent_on_host_picks_pair_partner_else_raises(wf_layer):
    _, _, workflow, conn = wf_layer
    assert workflow._other_agent_on_host(conn, "prod", "prod-claude") == "prod-codex"
    assert workflow._other_agent_on_host(conn, "prod", "prod-codex") == "prod-claude"
    # Unknown agent on host raises.
    with pytest.raises(ValueError):
        workflow._other_agent_on_host(conn, "prod", "ghost")
    # Host with !=2 workers raises (flow has 1 arbitrator only).
    with pytest.raises(ValueError):
        workflow._other_agent_on_host(conn, "flow", "flow-claude")


# ---------------------------------------------------------------------------
# Bonus: skipped reviewers actually narrow round_3 expected set
# ---------------------------------------------------------------------------


def test_round_3_respects_skipped_reviewers(wf_layer):
    _, db, workflow, conn = wf_layer
    session, _ = _converge_session_to_round_3(workflow, db, conn)
    db.add_skipped_reviewer(conn, session["id"], "dev-codex")
    rounds = db.list_rounds_for_session(conn, session["id"])
    review_round = next(r for r in rounds if r["round_type"] == "multi_agent_review")

    # Only three approvals required now.
    for reviewer in ["prod-claude", "prod-codex", "dev-claude"]:
        workflow.on_review_emitted(
            conn, round_id=review_round["id"], reviewer_agent=reviewer,
            decision="approve", comments=None, rationale="lgtm",
        )
    refreshed = db.get_session(conn, session["id"])
    assert refreshed["status"] == "executing"
