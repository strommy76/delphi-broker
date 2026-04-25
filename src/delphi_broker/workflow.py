"""Workflow engine for the v2 iterative-pipeline broker.

This module is the orchestrator that advances sessions through their state
machine. It does NOT poll external agents — agents pull from their inbox
(`find_inbox_for_agent`). The workflow module reacts to events:

  * `start_session`             — operator created a session
  * `on_destination_response`   — agent submitted a structured response
  * `on_review_emitted`         — round-3 reviewer submitted approve/reject
  * `on_executor_emitted`       — executor reported success/failure
  * `auto_skip_expired_nudges`  — periodic background sweep
  * `resolve_escalation`        — operator-driven recovery from a paused state

Functions are synchronous; the API/MCP layers are async but the workflow is
plain Python. Every public function takes `conn` as the first argument and
commits its own DB writes through the `database` module.

Fail-loud on contract violations (unknown session, malformed enum, missing
agents). Silent fallbacks are forbidden by project policy.
"""

from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from typing import Optional

from . import database as db
from .config import ARBITRATOR_AGENT_ID, EXECUTOR_AGENT_ID

# ---------------------------------------------------------------------------
# Constants (per DESIGN.md §5 / §8 / §9)
# ---------------------------------------------------------------------------

MAX_ITERS_PER_ROUND_1 = 8
MAX_MEDIATION_ATTEMPTS = 2
MAX_ESCALATIONS_PER_ROUND_3_REVIEWER = 3
CROSS_HOST_RECONCILABILITY_FLOOR = 0.30
CONVERGENCE_SIMILARITY_THRESHOLD = 0.95
OSCILLATION_SIMILARITY_CEILING = 0.50

CROSS_HOST_SEPARATOR = "\n---\n"
MEDIATION_PRIOR_LABEL = "Prior synthesis:"
MEDIATION_REJECTIONS_LABEL = "Reviewer rejections:"

WHITESPACE_NORM_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Convergence detection
# ---------------------------------------------------------------------------


def normalize_for_similarity(text: str) -> str:
    """Collapse runs of whitespace to a single space and strip ends."""
    return WHITESPACE_NORM_RE.sub(" ", text or "").strip()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(
        None, normalize_for_similarity(a), normalize_for_similarity(b)
    ).ratio()


def is_converged(
    prev_output: Optional[str], latest_output: str, latest_self_assess: str
) -> bool:
    """Hybrid: similarity >= 0.95 AND latest agent self-reports CONVERGED."""
    if prev_output is None:
        return False
    if latest_self_assess != "converged":
        return False
    return _similarity(prev_output, latest_output) >= CONVERGENCE_SIMILARITY_THRESHOLD


def detect_oscillation(outputs: list[str]) -> bool:
    """True if the last two iterations have similarity < 0.50."""
    if len(outputs) < 2:
        return False
    return _similarity(outputs[-2], outputs[-1]) < OSCILLATION_SIMILARITY_CEILING


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _worker_hosts(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute(
        """SELECT DISTINCT host FROM agents
            WHERE role = 'worker' ORDER BY host ASC"""
    )
    return [row["host"] for row in cur.fetchall()]


def _workers_on_host(conn: sqlite3.Connection, host: str) -> list[str]:
    cur = conn.execute(
        """SELECT agent_id FROM agents
            WHERE role = 'worker' AND host = ?
         ORDER BY agent_id ASC""",
        (host,),
    )
    return [row["agent_id"] for row in cur.fetchall()]


def _all_workers(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute(
        "SELECT agent_id FROM agents WHERE role = 'worker' ORDER BY agent_id ASC"
    )
    return [row["agent_id"] for row in cur.fetchall()]


def _other_agent_on_host(
    conn: sqlite3.Connection, host: str, current_agent: str
) -> str:
    """Return the worker on `host` that isn't `current_agent`.

    Raises ValueError unless exactly two workers exist on that host.
    """
    workers = _workers_on_host(conn, host)
    if len(workers) != 2:
        raise ValueError(
            f"host {host!r} must have exactly two workers, found {len(workers)}: "
            f"{workers!r}"
        )
    if current_agent not in workers:
        raise ValueError(
            f"agent {current_agent!r} is not a worker on host {host!r} "
            f"(workers: {workers!r})"
        )
    return next(w for w in workers if w != current_agent)


def _spawn_iteration(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    source_agent: Optional[str],
    destination_agent: str,
    source_output: str,
    nudge_window_secs: int,
) -> dict:
    """Determine the next iter_num for `round_id`, then create_iteration."""
    latest = db.latest_iteration_for_round(conn, round_id)
    iter_num = 1 if latest is None else latest["iter_num"] + 1
    return db.create_iteration(
        conn,
        round_id=round_id,
        iter_num=iter_num,
        source_agent=source_agent,
        destination_agent=destination_agent,
        source_output=source_output,
        nudge_window_secs=nudge_window_secs,
    )


def _round_outcomes_per_host(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Optional[str]]:
    """Map host -> outcome_text for the round_1 same_host_pair rounds."""
    rounds = db.list_rounds_for_session(conn, session_id)
    out: dict[str, Optional[str]] = {}
    for rnd in rounds:
        if rnd["round_type"] != "same_host_pair":
            continue
        out[rnd["host"]] = rnd["outcome_text"]
    return out


def _round_for_iteration(conn: sqlite3.Connection, iteration_id: str) -> dict:
    iteration = db.get_iteration(conn, iteration_id)
    if iteration is None:
        raise ValueError(f"unknown iteration_id {iteration_id!r}")
    rnd = db.get_round(conn, iteration["round_id"])
    if rnd is None:
        raise ValueError(f"iteration {iteration_id!r} has missing round")
    return rnd


def _session_for_round(conn: sqlite3.Connection, round_id: str) -> dict:
    rnd = db.get_round(conn, round_id)
    if rnd is None:
        raise ValueError(f"unknown round_id {round_id!r}")
    session = db.get_session(conn, rnd["session_id"])
    if session is None:
        raise ValueError(f"round {round_id!r} has missing session")
    return session


def _expected_reviewers_for_session(
    conn: sqlite3.Connection, session_id: str
) -> list[str]:
    """All workers minus this session's skipped_reviewers list."""
    skipped = set(db.get_skipped_reviewers(conn, session_id))
    return [w for w in _all_workers(conn) if w not in skipped]


def _format_arbitrator_input(host_outcomes: dict[str, Optional[str]]) -> str:
    """Format the cross-host source output for the arbitrator (round 2)."""
    pieces: list[str] = []
    for host in sorted(host_outcomes.keys()):
        outcome = host_outcomes[host] or ""
        pieces.append(f"[{host.upper()}]\n{outcome}")
    return CROSS_HOST_SEPARATOR.join(pieces)


def _format_mediation_input(
    prior_synthesis: str, rejections: list[tuple[str, Optional[str]]]
) -> str:
    lines = [f"{MEDIATION_PRIOR_LABEL} {prior_synthesis}", "", MEDIATION_REJECTIONS_LABEL]
    for reviewer, comments in rejections:
        lines.append(f"- {reviewer}: {comments or ''}")
    return "\n".join(lines)


def _outputs_for_round(conn: sqlite3.Connection, round_id: str) -> list[str]:
    iters = db.list_iterations_for_round(conn, round_id)
    return [
        i["destination_output"]
        for i in iters
        if i["destination_output"] is not None
    ]


def _mediation_attempt_count(conn: sqlite3.Connection, session_id: str) -> int:
    """Number of cross_host_arbitration rounds beyond the first."""
    cur = conn.execute(
        """SELECT COUNT(*) AS c FROM rounds
            WHERE session_id = ? AND round_type = 'cross_host_arbitration'""",
        (session_id,),
    )
    total = cur.fetchone()["c"]
    return max(0, total - 1)


# ---------------------------------------------------------------------------
# Session lifecycle controllers
# ---------------------------------------------------------------------------


def start_session(
    conn: sqlite3.Connection,
    *,
    problem_text: str,
    nudge_window_secs: int = 60,
    hosts: Optional[list[str]] = None,
) -> dict:
    """Create a session and spawn round_1 iterations on every worker host.

    `hosts` defaults to all distinct host values in the agents table where
    role='worker'. The first agent on each host (alphabetical) is the first
    destination; iteration alternates with the other agent on that host.
    """
    if hosts is None:
        hosts = _worker_hosts(conn)
    if not hosts:
        raise ValueError("cannot start session: no worker hosts registered")

    session = db.create_session(
        conn, problem_text=problem_text, nudge_window_secs=nudge_window_secs
    )

    for host in hosts:
        workers = _workers_on_host(conn, host)
        if len(workers) != 2:
            raise ValueError(
                f"host {host!r} must have exactly two workers, found {len(workers)}: "
                f"{workers!r}"
            )
        first_destination = workers[0]
        rnd = db.create_round(
            conn,
            session_id=session["id"],
            round_num=1,
            round_type="same_host_pair",
            host=host,
        )
        db.update_round_status(conn, rnd["id"], "in_progress")
        _spawn_iteration(
            conn,
            round_id=rnd["id"],
            source_agent=None,
            destination_agent=first_destination,
            source_output=problem_text,
            nudge_window_secs=nudge_window_secs,
        )

    return db.update_session_status(conn, session["id"], "round_1")


def on_destination_response(
    conn: sqlite3.Connection,
    iteration_id: str,
    *,
    output: str,
    self_assessment: str,
    rationale: Optional[str],
) -> dict:
    """Advance the workflow after an agent submits a structured response.

    Records the response, then either:
      * escalates the session (stall / oscillation / cross-host irreconcilable)
      * marks the round converged and (when applicable) advances to the next
        round_num
      * spawns the next iteration in the same round
    """
    db.record_destination_response(
        conn,
        iteration_id,
        output=output,
        self_assessment=self_assessment,
        rationale=rationale or "",
    )

    rnd = _round_for_iteration(conn, iteration_id)
    session = db.get_session(conn, rnd["session_id"])
    if session is None:
        raise ValueError(f"round {rnd['id']!r} has missing session")

    if rnd["round_type"] == "same_host_pair":
        return _advance_round_1(conn, session, rnd)

    if rnd["round_type"] == "cross_host_arbitration":
        return _advance_round_2(conn, session, rnd, output)

    raise ValueError(
        f"on_destination_response invoked for round_type {rnd['round_type']!r} "
        "which is not handled here"
    )


def _advance_round_1(
    conn: sqlite3.Connection, session: dict, rnd: dict
) -> dict:
    iters = db.list_iterations_for_round(conn, rnd["id"])
    completed = [i for i in iters if i["destination_output"] is not None]
    if not completed:
        raise RuntimeError("round_1 advance called with no completed iterations")
    latest = completed[-1]
    outputs = [i["destination_output"] for i in completed]

    # Stall: too many iterations without convergence.
    if latest["iter_num"] >= MAX_ITERS_PER_ROUND_1 and not is_converged(
        outputs[-2] if len(outputs) >= 2 else None,
        outputs[-1],
        latest["destination_self_assess"],
    ):
        db.update_round_status(conn, rnd["id"], "escalated")
        return db.update_session_status(conn, session["id"], "escalated")

    # Oscillation: two consecutive outputs diverge sharply.
    if detect_oscillation(outputs):
        db.update_round_status(conn, rnd["id"], "escalated")
        return db.update_session_status(conn, session["id"], "escalated")

    prev_output = outputs[-2] if len(outputs) >= 2 else None
    if is_converged(prev_output, outputs[-1], latest["destination_self_assess"]):
        db.update_round_status(
            conn, rnd["id"], "converged", outcome_text=outputs[-1]
        )
        return _maybe_advance_to_round_2(conn, session)

    # Continue iterating: source becomes the agent who just responded;
    # destination is the other agent on the same host.
    next_destination = _other_agent_on_host(
        conn, rnd["host"], latest["destination_agent"]
    )
    _spawn_iteration(
        conn,
        round_id=rnd["id"],
        source_agent=latest["destination_agent"],
        destination_agent=next_destination,
        source_output=outputs[-1],
        nudge_window_secs=session["nudge_window_secs"],
    )
    return db.get_session(conn, session["id"])


def _maybe_advance_to_round_2(conn: sqlite3.Connection, session: dict) -> dict:
    """If every round_1 same_host_pair round has converged, spawn round_2."""
    rounds = db.list_rounds_for_session(conn, session["id"])
    same_host = [r for r in rounds if r["round_type"] == "same_host_pair"]
    if not same_host:
        return db.get_session(conn, session["id"])
    if any(r["status"] != "converged" for r in same_host):
        return db.get_session(conn, session["id"])

    # Cross-host irreconcilability gate (pre-arbitration heuristic).
    outcomes = _round_outcomes_per_host(conn, session["id"])
    outcome_values = [v or "" for v in outcomes.values()]
    if len(outcome_values) >= 2:
        for i in range(len(outcome_values)):
            for j in range(i + 1, len(outcome_values)):
                if (
                    _similarity(outcome_values[i], outcome_values[j])
                    < CROSS_HOST_RECONCILABILITY_FLOOR
                ):
                    return db.update_session_status(
                        conn, session["id"], "escalated"
                    )

    return _spawn_round_2(conn, session, source_output=_format_arbitrator_input(outcomes))


def _spawn_round_2(
    conn: sqlite3.Connection, session: dict, *, source_output: str
) -> dict:
    rnd = db.create_round(
        conn,
        session_id=session["id"],
        round_num=2,
        round_type="cross_host_arbitration",
    )
    db.update_round_status(conn, rnd["id"], "in_progress")
    _spawn_iteration(
        conn,
        round_id=rnd["id"],
        source_agent=None,
        destination_agent=ARBITRATOR_AGENT_ID,
        source_output=source_output,
        nudge_window_secs=session["nudge_window_secs"],
    )
    return db.update_session_status(conn, session["id"], "round_2")


def _advance_round_2(
    conn: sqlite3.Connection, session: dict, rnd: dict, output: str
) -> dict:
    """Arbitrator submitted; capture outcome and spawn round_3 reviews."""
    db.update_round_status(conn, rnd["id"], "complete", outcome_text=output)
    return _spawn_round_3(conn, session, source_output=output)


def _spawn_round_3(
    conn: sqlite3.Connection, session: dict, *, source_output: str
) -> dict:
    reviewers = _expected_reviewers_for_session(conn, session["id"])
    if not reviewers:
        raise ValueError(
            f"session {session['id']!r} has no eligible reviewers — "
            "all workers have been skipped"
        )
    rnd = db.create_round(
        conn,
        session_id=session["id"],
        round_num=3,
        round_type="multi_agent_review",
    )
    db.update_round_status(conn, rnd["id"], "in_progress")
    for reviewer in reviewers:
        _spawn_iteration(
            conn,
            round_id=rnd["id"],
            source_agent=None,
            destination_agent=reviewer,
            source_output=source_output,
            nudge_window_secs=session["nudge_window_secs"],
        )
    return db.update_session_status(conn, session["id"], "round_3")


def on_review_emitted(
    conn: sqlite3.Connection,
    *,
    round_id: str,
    reviewer_agent: str,
    decision: str,
    comments: Optional[str],
    rationale: Optional[str],
) -> dict:
    """Round-3 reviewer submitted; advance once every reviewer is in."""
    rnd = db.get_round(conn, round_id)
    if rnd is None:
        raise ValueError(f"unknown round_id {round_id!r}")
    if rnd["round_type"] != "multi_agent_review":
        raise ValueError(
            f"round {round_id!r} is round_type {rnd['round_type']!r}, "
            "expected 'multi_agent_review'"
        )
    session = db.get_session(conn, rnd["session_id"])
    if session is None:
        raise ValueError(f"round {round_id!r} has missing session")

    db.create_review(
        conn,
        round_id=round_id,
        reviewer_agent=reviewer_agent,
        decision=decision,
        comments=comments,
        rationale=rationale,
    )

    expected = _expected_reviewers_for_session(conn, session["id"])
    pending = db.find_pending_reviewers_for_round(conn, round_id, expected)
    if pending:
        return db.get_session(conn, session["id"])

    reviews = db.list_reviews_for_round(conn, round_id)
    rejections = [(r["reviewer_agent"], r["comments"]) for r in reviews if r["decision"] == "reject"]

    if not rejections:
        db.update_round_status(conn, round_id, "complete")
        return _spawn_execute_round(conn, session)

    db.update_round_status(conn, round_id, "complete")

    attempts = _mediation_attempt_count(conn, session["id"])
    if attempts < MAX_MEDIATION_ATTEMPTS:
        prior = db.round_2_outcome_for_session(conn, session["id"]) or ""
        return _spawn_round_2(
            conn,
            session,
            source_output=_format_mediation_input(prior, rejections),
        )

    return _restart_round_1_with_comments(conn, session, rejections)


def _spawn_execute_round(conn: sqlite3.Connection, session: dict) -> dict:
    final_prompt = db.round_2_outcome_for_session(conn, session["id"]) or ""
    rnd = db.create_round(
        conn,
        session_id=session["id"],
        round_num=4,
        round_type="execute",
    )
    db.update_round_status(conn, rnd["id"], "in_progress")
    _spawn_iteration(
        conn,
        round_id=rnd["id"],
        source_agent=None,
        destination_agent=EXECUTOR_AGENT_ID,
        source_output=final_prompt,
        nudge_window_secs=session["nudge_window_secs"],
    )
    return db.update_session_status(conn, session["id"], "executing")


def _restart_round_1_with_comments(
    conn: sqlite3.Connection,
    session: dict,
    rejections: list[tuple[str, Optional[str]]],
) -> dict:
    """Full restart: spawn fresh round_1 rounds with all comments appended."""
    appendix = ["", "[Accumulated reviewer comments from prior round_3 attempts]"]
    for reviewer, comments in rejections:
        appendix.append(f"- {reviewer}: {comments or ''}")
    new_problem = session["problem_text"] + "\n" + "\n".join(appendix)

    hosts = _worker_hosts(conn)
    for host in hosts:
        workers = _workers_on_host(conn, host)
        if len(workers) != 2:
            raise ValueError(
                f"host {host!r} must have exactly two workers, found {len(workers)}"
            )
        rnd = db.create_round(
            conn,
            session_id=session["id"],
            round_num=1,
            round_type="same_host_pair",
            host=host,
        )
        db.update_round_status(conn, rnd["id"], "in_progress")
        _spawn_iteration(
            conn,
            round_id=rnd["id"],
            source_agent=None,
            destination_agent=workers[0],
            source_output=new_problem,
            nudge_window_secs=session["nudge_window_secs"],
        )
    return db.update_session_status(conn, session["id"], "round_1")


def on_executor_emitted(
    conn: sqlite3.Connection,
    *,
    iteration_id: str,
    success: bool,
    output: str,
    error: Optional[str],
) -> dict:
    """Capture the executor's result and either complete or escalate."""
    iteration = db.get_iteration(conn, iteration_id)
    if iteration is None:
        raise ValueError(f"unknown iteration_id {iteration_id!r}")
    rnd = db.get_round(conn, iteration["round_id"])
    if rnd is None:
        raise ValueError(f"iteration {iteration_id!r} has missing round")
    if rnd["round_type"] != "execute":
        raise ValueError(
            f"on_executor_emitted called for round_type {rnd['round_type']!r}"
        )
    session = db.get_session(conn, rnd["session_id"])
    if session is None:
        raise ValueError(f"round {rnd['id']!r} has missing session")

    if iteration["status"] == "awaiting_nudge":
        db.skip_nudge(conn, iteration_id)

    if success:
        db.record_destination_response(
            conn,
            iteration_id,
            output=output,
            self_assessment="converged",
            rationale="executor reported success",
        )
        db.update_round_status(conn, rnd["id"], "complete", outcome_text=output)
        if not session.get("finalized_prompt"):
            final = db.round_2_outcome_for_session(conn, session["id"]) or output
            db.set_finalized_prompt(conn, session["id"], final)
        return db.update_session_status(conn, session["id"], "complete")

    error_text = error or "executor reported failure"
    db.record_destination_response(
        conn,
        iteration_id,
        output=output or error_text,
        self_assessment="more_work_needed",
        rationale=error_text,
    )
    db.update_round_status(conn, rnd["id"], "escalated", outcome_text=error_text)
    return db.update_session_status(conn, session["id"], "escalated")


def auto_skip_expired_nudges(conn: sqlite3.Connection) -> int:
    """Skip every iteration whose nudge window has elapsed.

    Returns the number of iterations transitioned. Does not spawn destination
    work — agents will see status=awaiting_destination on their next inbox poll.
    """
    pending = db.find_pending_iterations(conn)
    count = 0
    for iteration in pending:
        db.skip_nudge(conn, iteration["id"])
        count += 1
    return count


# ---------------------------------------------------------------------------
# Operator escalation resolution
# ---------------------------------------------------------------------------


_RESOLVE_ACTIONS = {
    "force_converge",
    "retry",
    "abort",
    "skip_agent",
    "proceed_to_arbitrator",
}


def resolve_escalation(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    action: str,
    iteration_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    nudge_text: Optional[str] = None,
) -> dict:
    """Operator-driven recovery from a paused/escalated session.

    See module docstring / DESIGN.md §9 for valid actions and their meanings.
    Returns the updated session dict.
    """
    if action not in _RESOLVE_ACTIONS:
        raise ValueError(
            f"unknown escalation action {action!r}; valid: {sorted(_RESOLVE_ACTIONS)}"
        )
    session = db.get_session(conn, session_id)
    if session is None:
        raise ValueError(f"unknown session_id {session_id!r}")

    if action == "abort":
        return db.update_session_status(conn, session_id, "aborted")

    if action == "force_converge":
        return _force_converge_round_1(conn, session)

    if action == "retry":
        return _retry_iteration(conn, session, iteration_id, nudge_text)

    if action == "skip_agent":
        if not agent_id:
            raise ValueError("skip_agent requires agent_id")
        db.add_skipped_reviewer(conn, session_id, agent_id)
        return db.get_session(conn, session_id)

    if action == "proceed_to_arbitrator":
        return _proceed_to_arbitrator(conn, session)

    # Unreachable — guarded above.
    raise ValueError(f"unhandled action {action!r}")


def _force_converge_round_1(conn: sqlite3.Connection, session: dict) -> dict:
    rounds = db.list_rounds_for_session(conn, session["id"])
    same_host_open = [
        r
        for r in rounds
        if r["round_type"] == "same_host_pair"
        and r["status"] in ("in_progress", "escalated", "pending")
    ]
    if not same_host_open:
        raise ValueError(
            f"session {session['id']!r} has no in-progress round_1 rounds to force"
        )
    for rnd in same_host_open:
        outputs = _outputs_for_round(conn, rnd["id"])
        if not outputs:
            raise ValueError(
                f"round {rnd['id']!r} has no outputs to force-converge from"
            )
        db.update_round_status(
            conn, rnd["id"], "converged", outcome_text=outputs[-1]
        )
    return _maybe_advance_to_round_2(conn, db.get_session(conn, session["id"]))


def _retry_iteration(
    conn: sqlite3.Connection,
    session: dict,
    iteration_id: Optional[str],
    nudge_text: Optional[str],
) -> dict:
    if not iteration_id:
        raise ValueError("retry requires iteration_id")
    iteration = db.get_iteration(conn, iteration_id)
    if iteration is None:
        raise ValueError(f"unknown iteration_id {iteration_id!r}")
    rnd = db.get_round(conn, iteration["round_id"])
    if rnd is None:
        raise ValueError(f"iteration {iteration_id!r} has missing round")
    source_output = iteration["destination_output"] or iteration["source_output"]
    new_iter = _spawn_iteration(
        conn,
        round_id=rnd["id"],
        source_agent=iteration["destination_agent"],
        destination_agent=iteration["source_agent"] or iteration["destination_agent"],
        source_output=source_output,
        nudge_window_secs=session["nudge_window_secs"],
    )
    if nudge_text:
        db.apply_nudge(conn, new_iter["id"], nudge_text)

    if rnd["status"] in ("escalated", "pending"):
        db.update_round_status(conn, rnd["id"], "in_progress")
    if session["status"] == "escalated":
        return db.update_session_status(conn, session["id"], "round_1")
    return db.get_session(conn, session["id"])


def _proceed_to_arbitrator(conn: sqlite3.Connection, session: dict) -> dict:
    if session["status"] != "escalated":
        raise ValueError(
            f"proceed_to_arbitrator requires escalated session, got {session['status']!r}"
        )
    # Force-converge any open round_1 round whose status is not yet converged.
    rounds = db.list_rounds_for_session(conn, session["id"])
    for rnd in rounds:
        if rnd["round_type"] != "same_host_pair":
            continue
        if rnd["status"] == "converged":
            continue
        outputs = _outputs_for_round(conn, rnd["id"])
        if not outputs:
            raise ValueError(
                f"round {rnd['id']!r} has no outputs to proceed from"
            )
        db.update_round_status(
            conn, rnd["id"], "converged", outcome_text=outputs[-1]
        )
    outcomes = _round_outcomes_per_host(conn, session["id"])
    return _spawn_round_2(
        conn, session, source_output=_format_arbitrator_input(outcomes)
    )
