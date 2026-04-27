"""v3 MCP tools — orchestrator + worker surfaces.

Mounted on the existing FastMCP instance via register_v3_tools(mcp). All
tools are HMAC-authenticated via the same _verify helper v2 uses; only
the action name and signed-fields tuple change per tool.

Signature canonicals (the operator's bootstrap reference):

  v3_get_pending_task | <agent_id> | <client_ts>
  v3_dispatch         | <agent_id> | <client_ts> | <task_id> | <worker_id>
  v3_collect_outputs  | <agent_id> | <client_ts> | <task_id>
  v3_aggregate        | <agent_id> | <client_ts> | <task_id> | <decision>
  v3_poll_dispatches  | <agent_id> | <client_ts>
  v3_emit_output      | <agent_id> | <client_ts> | <dispatch_id>

The pipe-separator + ordered fields convention matches v2's signing
(see `database.compute_signature`). Agents that already know how to sign
v2 actions need only to learn the v3 action names and field orders.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import database as v3db


def register_v3_tools(mcp, _verify, _conn, AGENT_SECRETS) -> None:
    """Register the six v3 tools on a FastMCP instance.

    Parameters are passed in to avoid circular imports — the caller hands us
    its existing _verify helper, _conn factory, and AGENT_SECRETS map. This
    keeps v3 cleanly decoupled from mcp_server module-level state.
    """

    # -----------------------------------------------------------------------
    # Orchestrator tools
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="delphi_v3_get_pending_task",
        description=(
            "Orchestrator polls for tasks awaiting their decisions. Returns "
            "tasks where the calling agent is the orchestrator and the task "
            "is not yet terminal (status in {new, dispatched, aggregating, "
            "escalated}). Caller is the orchestrator. Signature canonical: "
            "'v3_get_pending_task|<agent_id>|<client_ts>'."
        ),
    )
    def delphi_v3_get_pending_task(
        agent_id: str, client_ts: str, signature: str,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn, agent_id, client_ts, signature,
                ("v3_get_pending_task", agent_id, client_ts),
            )
            if err:
                return err

            tasks = v3db.list_tasks(conn, orchestrator_id=agent_id, limit=200)
            active = [
                t for t in tasks
                if t["status"] in ("new", "dispatched", "aggregating", "escalated")
            ]
            return {"tasks": active}
        finally:
            conn.close()

    @mcp.tool(
        name="delphi_v3_dispatch",
        description=(
            "Orchestrator dispatches a subtask to a worker. Independence rule "
            "is enforced: worker_id != orchestrator_id. The orchestrator must "
            "be the task's orchestrator (verified server-side). Signature "
            "canonical: 'v3_dispatch|<agent_id>|<client_ts>|<task_id>|<worker_id>'."
        ),
    )
    def delphi_v3_dispatch(
        agent_id: str, client_ts: str, signature: str,
        task_id: str, worker_id: str, subtask_text: str,
        subtask_json: dict | None = None,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn, agent_id, client_ts, signature,
                ("v3_dispatch", agent_id, client_ts, task_id, worker_id),
            )
            if err:
                return err

            task = v3db.get_task(conn, task_id)
            if task is None:
                return {"error": "not_found", "reason": f"task {task_id!r} not found"}
            if task["orchestrator_id"] != agent_id:
                return {
                    "error": "forbidden",
                    "reason": (
                        f"agent {agent_id!r} is not the orchestrator for task "
                        f"{task_id!r} (orchestrator is {task['orchestrator_id']!r})"
                    ),
                }
            if task["status"] in ("complete", "aborted"):
                return {
                    "error": "conflict",
                    "reason": f"task is in terminal status {task['status']!r}",
                }
            try:
                dispatch_id = v3db.create_dispatch(
                    conn, task_id=task_id, worker_id=worker_id,
                    subtask_text=subtask_text, subtask_json=subtask_json,
                    actor=agent_id,
                )
            except ValueError as exc:
                return {"error": "invalid", "reason": str(exc)}
            return {
                "dispatch_id": dispatch_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "status": "pending",
            }
        finally:
            conn.close()

    @mcp.tool(
        name="delphi_v3_collect_outputs",
        description=(
            "Orchestrator reads all worker outputs received so far for a task. "
            "Caller must be the task's orchestrator. Signature canonical: "
            "'v3_collect_outputs|<agent_id>|<client_ts>|<task_id>'."
        ),
    )
    def delphi_v3_collect_outputs(
        agent_id: str, client_ts: str, signature: str, task_id: str,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn, agent_id, client_ts, signature,
                ("v3_collect_outputs", agent_id, client_ts, task_id),
            )
            if err:
                return err

            task = v3db.get_task(conn, task_id)
            if task is None:
                return {"error": "not_found", "reason": f"task {task_id!r} not found"}
            if task["orchestrator_id"] != agent_id:
                return {
                    "error": "forbidden",
                    "reason": f"agent {agent_id!r} is not the orchestrator for task {task_id!r}",
                }

            dispatches = v3db.list_dispatches(conn, task_id=task_id)
            outputs = v3db.get_outputs_for_task(conn, task_id)
            return {
                "task_id": task_id,
                "task_status": task["status"],
                "dispatches": dispatches,
                "outputs": outputs,
            }
        finally:
            conn.close()

    @mcp.tool(
        name="delphi_v3_aggregate",
        description=(
            "Orchestrator submits a synthesis + decision (done/refine/escalate). "
            "Caller must be the task's orchestrator. The decision drives the "
            "task's status transition: done -> awaiting_approval, refine -> "
            "stays dispatched, escalate -> escalated. Signature canonical: "
            "'v3_aggregate|<agent_id>|<client_ts>|<task_id>|<decision>'."
        ),
    )
    def delphi_v3_aggregate(
        agent_id: str, client_ts: str, signature: str,
        task_id: str, synthesis_text: str, decision: str,
        refine_directive: str | None = None,
        synthesis_json: dict | None = None,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn, agent_id, client_ts, signature,
                ("v3_aggregate", agent_id, client_ts, task_id, decision),
            )
            if err:
                return err

            task = v3db.get_task(conn, task_id)
            if task is None:
                return {"error": "not_found", "reason": f"task {task_id!r} not found"}
            if task["orchestrator_id"] != agent_id:
                return {
                    "error": "forbidden",
                    "reason": f"agent {agent_id!r} is not the orchestrator for task {task_id!r}",
                }
            try:
                agg_id = v3db.create_aggregation(
                    conn, task_id=task_id,
                    synthesis_text=synthesis_text,
                    synthesis_json=synthesis_json,
                    decision=decision,
                    refine_directive=refine_directive,
                    actor=agent_id,
                )
            except ValueError as exc:
                return {"error": "invalid", "reason": str(exc)}

            updated = v3db.get_task(conn, task_id)
            return {
                "aggregation_id": agg_id,
                "task_id": task_id,
                "decision": decision,
                "task_status": updated["status"] if updated else None,
            }
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Worker tools
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="delphi_v3_poll_dispatches",
        description=(
            "Worker polls for pending or in-progress dispatches assigned to "
            "them. Use this each cycle to discover new work. Signature "
            "canonical: 'v3_poll_dispatches|<agent_id>|<client_ts>'."
        ),
    )
    def delphi_v3_poll_dispatches(
        agent_id: str, client_ts: str, signature: str,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn, agent_id, client_ts, signature,
                ("v3_poll_dispatches", agent_id, client_ts),
            )
            if err:
                return err

            pending = v3db.list_dispatches(conn, worker_id=agent_id, status="pending")
            in_progress = v3db.list_dispatches(conn, worker_id=agent_id, status="in_progress")
            # Each dispatch references its task; surface the task's problem text
            # so the worker has full context without a follow-up call.
            results: list[dict[str, Any]] = []
            for d in pending + in_progress:
                task = v3db.get_task(conn, d["task_id"])
                results.append({
                    "dispatch_id": d["id"],
                    "task_id": d["task_id"],
                    "task_title": task["title"] if task else None,
                    "task_problem_text": task["problem_text"] if task else None,
                    "subtask_text": d["subtask_text"],
                    "subtask_json": d["subtask_json"],
                    "status": d["status"],
                    "dispatched_at": d["dispatched_at"],
                })
            return {"dispatches": results}
        finally:
            conn.close()

    @mcp.tool(
        name="delphi_v3_emit_output",
        description=(
            "Worker submits their response to a dispatch. The dispatch is "
            "marked done. Caller must be the dispatch's assigned worker. "
            "Signature canonical: 'v3_emit_output|<agent_id>|<client_ts>|<dispatch_id>'."
        ),
    )
    def delphi_v3_emit_output(
        agent_id: str, client_ts: str, signature: str,
        dispatch_id: str, output_text: str,
        output_json: dict | None = None,
    ) -> dict:
        conn = _conn()
        try:
            err = _verify(
                conn, agent_id, client_ts, signature,
                ("v3_emit_output", agent_id, client_ts, dispatch_id),
            )
            if err:
                return err

            dispatch = v3db.get_dispatch(conn, dispatch_id)
            if dispatch is None:
                return {"error": "not_found", "reason": f"dispatch {dispatch_id!r} not found"}
            if dispatch["worker_id"] != agent_id:
                return {
                    "error": "forbidden",
                    "reason": (
                        f"agent {agent_id!r} is not the assigned worker for "
                        f"dispatch {dispatch_id!r} (worker is {dispatch['worker_id']!r})"
                    ),
                }
            if dispatch["status"] in ("done", "cancelled"):
                return {
                    "error": "conflict",
                    "reason": f"dispatch is in terminal status {dispatch['status']!r}",
                }
            try:
                output_id = v3db.record_worker_output(
                    conn, dispatch_id=dispatch_id,
                    output_text=output_text, output_json=output_json,
                )
            except ValueError as exc:
                return {"error": "invalid", "reason": str(exc)}
            return {
                "output_id": output_id,
                "dispatch_id": dispatch_id,
                "task_id": dispatch["task_id"],
                "status": "done",
            }
        finally:
            conn.close()
