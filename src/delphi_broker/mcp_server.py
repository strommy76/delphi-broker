"""MCP tools for the v2 broker.

Exposes four tools to agents:

  * `delphi_poll_inbox`     — return open work for the calling agent
  * `delphi_emit_response`  — worker / arbitrator submits a structured response
  * `delphi_emit_review`    — round-3 reviewer submits APPROVE / REJECT
  * `delphi_executor_emit`  — executor reports success / failure of the final run

Every tool requires:

  1. Agent identity verified against the agents table (`db.verify_agent`).
  2. `client_ts` within the replay window (`db.check_timestamp_freshness`).
  3. HMAC-SHA256 signature matching the canonical field set for that action,
     using the agent's secret from `config.AGENT_SECRETS`.

On any verification failure the tool returns `{"error": "auth_failed", ...}`
without dispatching into workflow.* — fail-loud per project policy.
"""

from __future__ import annotations

import hmac
import sqlite3
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import database as db
from . import workflow
from .config import AGENT_SECRETS, ARBITRATOR_AGENT_ID, DB_PATH, EXECUTOR_AGENT_ID

# DNS-rebinding protection in FastMCP defaults to localhost-only, which
# blocks every agent reaching us over Tailscale (Host header = the broker's
# tailnet IP). The HMAC + Tailscale-mesh boundary already authenticates
# agents, so we allow all hosts by default; override via env if you want
# stricter isolation.
_allowed_hosts_env = os.getenv("DELPHI_ALLOWED_HOSTS", "*").strip()
_allowed_hosts = (
    ["*"] if _allowed_hosts_env == "*"
    else [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
)
_allowed_origins_env = os.getenv("DELPHI_ALLOWED_ORIGINS", "*").strip()
_allowed_origins = (
    ["*"] if _allowed_origins_env == "*"
    else [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
)

mcp = FastMCP(
    "delphi-broker",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=(_allowed_hosts != ["*"]),
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)

# Phase-1 push spike: register four test tools that exercise different
# server->client notification paths. Used to empirically validate which
# wake-up primitives surface in Claude Code / Codex MCP clients before
# we commit to a v3 push design. Spike tools are unauthenticated for
# convenience -- they're informational, not state-mutating.
# v3 production tools (delphi_v3_*) are registered at the bottom of the
# file, after _verify and _conn helpers are defined.
from .v3.push_spike import register_spike_tools  # noqa: E402
register_spike_tools(mcp)


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    return db.get_connection(DB_PATH)


def _auth_failed(reason: str) -> dict:
    """Standard response shape for any auth/verification failure."""
    return {"error": "auth_failed", "reason": reason}


def _verify(
    conn: sqlite3.Connection,
    agent_id: str,
    client_ts: str,
    signature: str,
    fields: tuple[str, ...],
) -> Optional[dict]:
    """Run all auth checks; return None on success or an auth_failed dict."""
    if not agent_id:
        return _auth_failed("missing agent_id")
    if not db.verify_agent(conn, agent_id):
        return _auth_failed(f"unknown agent_id {agent_id!r}")
    secret = AGENT_SECRETS.get(agent_id)
    if not secret:
        return _auth_failed(f"no secret configured for agent {agent_id!r}")
    if not client_ts or not signature:
        return _auth_failed("missing client_ts or signature")
    if not db.check_timestamp_freshness(client_ts):
        return _auth_failed("client_ts outside replay window")
    expected = db.compute_signature(secret, *fields)
    if not hmac.compare_digest(signature, expected):
        return _auth_failed("invalid signature")
    return None


# ---------------------------------------------------------------------------
# Inbox helpers
# ---------------------------------------------------------------------------


def _format_inbox_iteration(conn: sqlite3.Connection, iteration: dict) -> dict:
    rnd = db.get_round(conn, iteration["round_id"])
    return {
        "request_id": iteration["id"],
        "session_id": rnd["session_id"] if rnd else None,
        "round_num": rnd["round_num"] if rnd else None,
        "round_type": rnd["round_type"] if rnd else None,
        "input_text": iteration["source_output"],
        "nudge": iteration.get("nudge_text"),
        "deadline": None,
    }


def _open_review_requests_for_agent(
    conn: sqlite3.Connection, agent_id: str
) -> list[dict]:
    """Round-3 review rounds where this agent is expected and hasn't reviewed."""
    cur = conn.execute(
        """SELECT r.* FROM rounds r
            WHERE r.round_type = 'multi_agent_review'
              AND r.status = 'in_progress'"""
    )
    out: list[dict] = []
    for row in cur.fetchall():
        rnd = dict(row)
        try:
            skipped = set(db.get_skipped_reviewers(conn, rnd["session_id"]))
        except ValueError:
            skipped = set()
        if agent_id in skipped:
            continue
        # The expected reviewers are computed via the workflow rule (all
        # workers minus skipped). Mirror that here without importing the
        # internal helper from workflow.
        worker_cur = conn.execute(
            "SELECT agent_id FROM agents WHERE role = 'worker' ORDER BY agent_id ASC"
        )
        workers = [w["agent_id"] for w in worker_cur.fetchall()]
        expected = [w for w in workers if w not in skipped]
        if agent_id not in expected:
            continue
        pending = db.find_pending_reviewers_for_round(conn, rnd["id"], expected)
        if agent_id not in pending:
            continue
        outcome = db.round_2_outcome_for_session(conn, rnd["session_id"]) or ""
        out.append(
            {
                "request_id": rnd["id"],
                "session_id": rnd["session_id"],
                "input_text": outcome,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def delphi_poll_inbox(agent_id: str, client_ts: str, signature: str) -> dict:
    """Return the agent's open work items.

    Signature: HMAC-SHA256(secret, "poll_inbox|agent_id|client_ts").

    Iterations: any awaiting_destination record whose destination_agent matches
    the caller. For arbitrator/executor this is the same lookup since they are
    addressed via destination_agent.

    Reviews pending: round_3 rounds in progress where the caller is in the
    expected reviewer set and hasn't yet emitted a review.
    """
    conn = _conn()
    try:
        err = _verify(
            conn,
            agent_id,
            client_ts,
            signature,
            ("poll_inbox", agent_id, client_ts),
        )
        if err:
            return err
        db.touch_agent(conn, agent_id)
        iterations_raw = db.find_inbox_for_agent(conn, agent_id)
        iterations = [_format_inbox_iteration(conn, it) for it in iterations_raw]
        reviews_pending = _open_review_requests_for_agent(conn, agent_id)
        return {"iterations": iterations, "reviews_pending": reviews_pending}
    finally:
        conn.close()


@mcp.tool()
def delphi_emit_response(
    agent_id: str,
    request_id: str,
    client_ts: str,
    signature: str,
    output: str,
    self_assessment: str,
    rationale: str = "",
) -> dict:
    """Worker or arbitrator submits a structured response to an iteration.

    `request_id` is the iteration_id. `self_assessment` must be one of
    'converged' or 'more_work_needed'. The arbitrator uses this same tool —
    the iteration's destination_agent identifies who is expected to respond.
    """
    conn = _conn()
    try:
        err = _verify(
            conn,
            agent_id,
            client_ts,
            signature,
            db.build_emit_response_signature_fields(
                agent_id=agent_id,
                iteration_id=request_id,
                timestamp=client_ts,
                output=output,
                self_assessment=self_assessment,
                rationale=rationale,
            ),
        )
        if err:
            return err
        iteration = db.get_iteration(conn, request_id)
        if iteration is None:
            return _auth_failed(f"unknown iteration {request_id!r}")
        if iteration["destination_agent"] != agent_id:
            return _auth_failed(
                f"agent {agent_id!r} is not the destination for iteration "
                f"{request_id!r} (destination is {iteration['destination_agent']!r})"
            )
        db.touch_agent(conn, agent_id)
        try:
            session = workflow.on_destination_response(
                conn,
                request_id,
                output=output,
                self_assessment=self_assessment,
                rationale=rationale or None,
            )
        except ValueError as exc:
            return {"error": "workflow_rejected", "reason": str(exc)}
        return {"ok": True, "session_status": session["status"]}
    finally:
        conn.close()


@mcp.tool()
def delphi_emit_review(
    agent_id: str,
    request_id: str,
    client_ts: str,
    signature: str,
    decision: str,
    comments: str = "",
    rationale: str = "",
) -> dict:
    """Round-3 reviewer submits APPROVE or REJECT.

    `request_id` is the round_id (multi_agent_review). `decision` is
    case-insensitive: APPROVE / REJECT / approve / reject all accepted.
    """
    conn = _conn()
    try:
        normalized = (decision or "").strip().lower()
        if normalized not in ("approve", "reject"):
            return _auth_failed(f"invalid decision {decision!r}")
        err = _verify(
            conn,
            agent_id,
            client_ts,
            signature,
            db.build_emit_review_signature_fields(
                agent_id=agent_id,
                round_id=request_id,
                timestamp=client_ts,
                decision=normalized,
                comments=comments or None,
                rationale=rationale or None,
            ),
        )
        if err:
            return err
        rnd = db.get_round(conn, request_id)
        if rnd is None:
            return _auth_failed(f"unknown round {request_id!r}")
        if rnd["round_type"] != "multi_agent_review":
            return _auth_failed(
                f"round {request_id!r} is not a multi_agent_review "
                f"(round_type={rnd['round_type']!r})"
            )
        db.touch_agent(conn, agent_id)
        try:
            session = workflow.on_review_emitted(
                conn,
                round_id=request_id,
                reviewer_agent=agent_id,
                decision=normalized,
                comments=comments or None,
                rationale=rationale or None,
            )
        except ValueError as exc:
            return {"error": "workflow_rejected", "reason": str(exc)}
        return {"ok": True, "session_status": session["status"]}
    finally:
        conn.close()


@mcp.tool()
def delphi_executor_emit(
    agent_id: str,
    request_id: str,
    client_ts: str,
    signature: str,
    success: bool,
    output: str,
    error: str = "",
) -> dict:
    """Executor reports the result of running the finalized prompt."""
    conn = _conn()
    try:
        if agent_id != EXECUTOR_AGENT_ID:
            return _auth_failed(
                f"agent {agent_id!r} is not the configured executor "
                f"({EXECUTOR_AGENT_ID!r})"
            )
        err = _verify(
            conn,
            agent_id,
            client_ts,
            signature,
            db.build_executor_emit_signature_fields(
                agent_id=agent_id,
                iteration_id=request_id,
                timestamp=client_ts,
                success=success,
                output=output,
                error=error or None,
            ),
        )
        if err:
            return err
        iteration = db.get_iteration(conn, request_id)
        if iteration is None:
            return _auth_failed(f"unknown iteration {request_id!r}")
        if iteration["destination_agent"] != agent_id:
            return _auth_failed(
                f"agent {agent_id!r} is not the destination for iteration "
                f"{request_id!r}"
            )
        db.touch_agent(conn, agent_id)
        try:
            session = workflow.on_executor_emitted(
                conn,
                iteration_id=request_id,
                success=success,
                output=output,
                error=error or None,
            )
        except ValueError as exc:
            return {"error": "workflow_rejected", "reason": str(exc)}
        return {"ok": True, "session_status": session["status"]}
    finally:
        conn.close()


# v3 production tools — orchestrator + worker surfaces. Registered here
# (end of module) so that _verify, _conn, and AGENT_SECRETS are all in
# scope. The v3 module deliberately accepts these as parameters to stay
# decoupled from module-level state.
from .v3.mcp_tools import register_v3_tools  # noqa: E402
register_v3_tools(mcp, _verify, _conn, AGENT_SECRETS)


# Re-exported for tests: sentinel constants the test suite asserts against.
__all__ = [
    "ARBITRATOR_AGENT_ID",
    "EXECUTOR_AGENT_ID",
    "delphi_poll_inbox",
    "delphi_emit_response",
    "delphi_emit_review",
    "delphi_executor_emit",
    "mcp",
]
