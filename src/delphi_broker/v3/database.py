"""v3 schema + DAO — orchestrator-worker pattern.

Schema (all tables prefixed v3_ to make migration / archive boundaries
explicit; agents and agent secrets remain shared with v2):

  v3_tasks            — operator's unit of work, owned by one orchestrator
  v3_dispatches       — orchestrator -> worker subtask assignments
  v3_worker_outputs   — workers' responses to dispatches
  v3_aggregations     — orchestrator's syntheses + decisions
  v3_task_events      — append-only audit log

The eligibility rule (per project memory): once an agent is chosen as
the orchestrator for a task, they're precluded from worker roles in
that task. eligible_workers(task) = agents - {orchestrator_id}. This
is enforced at dispatch-creation time in `create_dispatch`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS v3_tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    problem_text    TEXT NOT NULL,
    task_json       TEXT,
    orchestrator_id TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN (
        'new', 'dispatched', 'aggregating', 'awaiting_approval',
        'complete', 'aborted', 'escalated'
    )),
    final_artifact      TEXT,
    final_artifact_json TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(orchestrator_id) REFERENCES agents(agent_id)
);

CREATE TABLE IF NOT EXISTS v3_dispatches (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    worker_id     TEXT NOT NULL,
    subtask_text  TEXT NOT NULL,
    subtask_json  TEXT,
    status        TEXT NOT NULL CHECK(status IN (
        'pending', 'in_progress', 'done', 'failed', 'cancelled'
    )),
    dispatched_at TEXT NOT NULL,
    received_at   TEXT,
    completed_at  TEXT,
    FOREIGN KEY(task_id)   REFERENCES v3_tasks(id),
    FOREIGN KEY(worker_id) REFERENCES agents(agent_id)
);

CREATE TABLE IF NOT EXISTS v3_worker_outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    output_text TEXT NOT NULL,
    output_json TEXT,
    emitted_at  TEXT NOT NULL,
    FOREIGN KEY(dispatch_id) REFERENCES v3_dispatches(id)
);

CREATE TABLE IF NOT EXISTS v3_aggregations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    synthesis_text  TEXT NOT NULL,
    synthesis_json  TEXT,
    decision        TEXT NOT NULL CHECK(decision IN ('done', 'refine', 'escalate')),
    refine_directive TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES v3_tasks(id)
);

CREATE TABLE IF NOT EXISTS v3_task_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    actor        TEXT,
    payload_json TEXT,
    occurred_at  TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES v3_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_v3_tasks_status   ON v3_tasks(status);
CREATE INDEX IF NOT EXISTS idx_v3_dispatches_task   ON v3_dispatches(task_id);
CREATE INDEX IF NOT EXISTS idx_v3_dispatches_worker ON v3_dispatches(worker_id, status);
CREATE INDEX IF NOT EXISTS idx_v3_outputs_dispatch  ON v3_worker_outputs(dispatch_id);
CREATE INDEX IF NOT EXISTS idx_v3_aggregations_task ON v3_aggregations(task_id);
CREATE INDEX IF NOT EXISTS idx_v3_events_task ON v3_task_events(task_id, occurred_at);
"""


def init_v3_schema(conn: sqlite3.Connection) -> None:
    """Apply v3 schema. Idempotent. Runs alongside v2 init."""
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(c: sqlite3.Cursor) -> dict[str, Any] | None:
    row = c.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in c.description]
    return dict(zip(cols, row))


def _rows(c: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, r)) for r in c.fetchall()]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    problem_text: str,
    orchestrator_id: str,
    task_json: dict[str, Any] | None = None,
) -> str:
    """Create a new task. Returns task_id (UUID).

    Raises ValueError if orchestrator_id is not registered as an agent.
    """
    cur = conn.execute(
        "SELECT 1 FROM agents WHERE agent_id = ?", (orchestrator_id,)
    )
    if cur.fetchone() is None:
        raise ValueError(f"orchestrator_id {orchestrator_id!r} is not a registered agent")

    task_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO v3_tasks "
        "(id, title, problem_text, task_json, orchestrator_id, status, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
        (
            task_id, title, problem_text,
            json.dumps(task_json) if task_json else None,
            orchestrator_id, now, now,
        ),
    )
    log_event(
        conn, task_id=task_id, event_type="task_created",
        actor="operator",
        payload={"title": title, "orchestrator_id": orchestrator_id},
    )
    conn.commit()
    return task_id


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM v3_tasks WHERE id = ?", (task_id,))
    return _row(cur)


def list_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    orchestrator_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if orchestrator_id is not None:
        where.append("orchestrator_id = ?")
        params.append(orchestrator_id)
    sql = "SELECT * FROM v3_tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return _rows(cur)


_VALID_TASK_TRANSITIONS: dict[str, set[str]] = {
    "new":               {"dispatched", "aborted"},
    "dispatched":        {"aggregating", "awaiting_approval", "dispatched",
                          "aborted", "escalated"},  # self-loop allowed for refine
    "aggregating":       {"dispatched", "awaiting_approval", "aborted", "escalated"},
    "awaiting_approval": {"complete", "dispatched", "aborted"},  # operator can refine
    "complete":          set(),
    "aborted":           set(),
    "escalated":         {"aborted", "dispatched"},  # operator can resolve
}


def update_task_status(
    conn: sqlite3.Connection, task_id: str, new_status: str,
    *, actor: str | None = None,
) -> None:
    """Move a task to a new status. Validates transition. Same-status is a no-op."""
    cur = conn.execute("SELECT status FROM v3_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"task {task_id!r} not found")
    current = row[0]
    if current == new_status:
        return  # idempotent no-op
    if new_status not in _VALID_TASK_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"invalid task transition: {current!r} -> {new_status!r}"
        )
    now = _now()
    update_completed = ", completed_at = ?" if new_status in ("complete", "aborted") else ""
    update_args: list[Any] = [new_status, now]
    if new_status in ("complete", "aborted"):
        update_args.append(now)
    update_args.append(task_id)
    conn.execute(
        f"UPDATE v3_tasks SET status = ?, updated_at = ?{update_completed} WHERE id = ?",
        update_args,
    )
    log_event(
        conn, task_id=task_id, event_type="task_status_changed",
        actor=actor,
        payload={"from": current, "to": new_status},
    )
    conn.commit()


def finalize_task(
    conn: sqlite3.Connection, task_id: str,
    *, final_artifact: str, final_artifact_json: dict[str, Any] | None = None,
    actor: str = "operator",
) -> None:
    """Mark a task complete with an approved final artifact. Operator action."""
    now = _now()
    conn.execute(
        "UPDATE v3_tasks SET status = 'complete', final_artifact = ?, "
        "final_artifact_json = ?, completed_at = ?, updated_at = ? "
        "WHERE id = ?",
        (
            final_artifact,
            json.dumps(final_artifact_json) if final_artifact_json else None,
            now, now, task_id,
        ),
    )
    log_event(
        conn, task_id=task_id, event_type="task_finalized",
        actor=actor,
        payload={"artifact_len": len(final_artifact)},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Dispatches
# ---------------------------------------------------------------------------


def create_dispatch(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    worker_id: str,
    subtask_text: str,
    subtask_json: dict[str, Any] | None = None,
    actor: str | None = None,
) -> str:
    """Create a dispatch. Enforces: worker_id != task.orchestrator_id."""
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id!r} not found")
    if worker_id == task["orchestrator_id"]:
        raise ValueError(
            f"orchestrator {worker_id!r} cannot dispatch to themselves "
            "(independence rule)"
        )
    cur = conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (worker_id,))
    if cur.fetchone() is None:
        raise ValueError(f"worker_id {worker_id!r} is not a registered agent")

    dispatch_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO v3_dispatches "
        "(id, task_id, worker_id, subtask_text, subtask_json, status, dispatched_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (
            dispatch_id, task_id, worker_id, subtask_text,
            json.dumps(subtask_json) if subtask_json else None,
            now,
        ),
    )
    # If task was 'new', advance to 'dispatched'
    if task["status"] == "new":
        update_task_status(conn, task_id, "dispatched", actor=actor or task["orchestrator_id"])
    log_event(
        conn, task_id=task_id, event_type="dispatch_created",
        actor=actor or task["orchestrator_id"],
        payload={"dispatch_id": dispatch_id, "worker_id": worker_id},
    )
    conn.commit()
    return dispatch_id


def get_dispatch(conn: sqlite3.Connection, dispatch_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM v3_dispatches WHERE id = ?", (dispatch_id,))
    return _row(cur)


def list_dispatches(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    worker_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if worker_id is not None:
        where.append("worker_id = ?")
        params.append(worker_id)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM v3_dispatches"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY dispatched_at DESC LIMIT ?"
    params.append(int(limit))
    return _rows(conn.execute(sql, params))


_VALID_DISPATCH_TRANSITIONS: dict[str, set[str]] = {
    "pending":     {"in_progress", "cancelled"},
    "in_progress": {"done", "failed", "cancelled"},
    "done":        set(),
    "failed":      {"in_progress"},   # allow retry
    "cancelled":   set(),
}


def update_dispatch_status(
    conn: sqlite3.Connection, dispatch_id: str, new_status: str,
    *, actor: str | None = None,
) -> None:
    cur = conn.execute(
        "SELECT status, task_id FROM v3_dispatches WHERE id = ?", (dispatch_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"dispatch {dispatch_id!r} not found")
    current, task_id = row[0], row[1]
    if new_status not in _VALID_DISPATCH_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"invalid dispatch transition: {current!r} -> {new_status!r}"
        )
    now = _now()
    if new_status == "in_progress" and current == "pending":
        conn.execute(
            "UPDATE v3_dispatches SET status = ?, received_at = ? WHERE id = ?",
            (new_status, now, dispatch_id),
        )
    elif new_status in ("done", "failed", "cancelled"):
        conn.execute(
            "UPDATE v3_dispatches SET status = ?, completed_at = ? WHERE id = ?",
            (new_status, now, dispatch_id),
        )
    else:
        conn.execute(
            "UPDATE v3_dispatches SET status = ? WHERE id = ?",
            (new_status, dispatch_id),
        )
    log_event(
        conn, task_id=task_id, event_type="dispatch_status_changed",
        actor=actor,
        payload={"dispatch_id": dispatch_id, "from": current, "to": new_status},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Worker outputs
# ---------------------------------------------------------------------------


def record_worker_output(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    output_text: str,
    output_json: dict[str, Any] | None = None,
) -> int:
    """Store a worker's output and mark its dispatch done. Returns row id."""
    dispatch = get_dispatch(conn, dispatch_id)
    if dispatch is None:
        raise ValueError(f"dispatch {dispatch_id!r} not found")

    now = _now()
    cur = conn.execute(
        "INSERT INTO v3_worker_outputs (dispatch_id, output_text, output_json, emitted_at) "
        "VALUES (?, ?, ?, ?)",
        (
            dispatch_id, output_text,
            json.dumps(output_json) if output_json else None,
            now,
        ),
    )
    output_id = int(cur.lastrowid)
    update_dispatch_status(
        conn, dispatch_id, "done", actor=dispatch["worker_id"],
    )
    log_event(
        conn, task_id=dispatch["task_id"], event_type="worker_output_received",
        actor=dispatch["worker_id"],
        payload={"dispatch_id": dispatch_id, "output_id": output_id, "output_len": len(output_text)},
    )
    conn.commit()
    return output_id


def get_outputs_for_task(
    conn: sqlite3.Connection, task_id: str,
) -> list[dict[str, Any]]:
    """Return all worker outputs for a task, joined with their dispatch info."""
    cur = conn.execute(
        "SELECT o.*, d.worker_id, d.subtask_text, d.subtask_json "
        "FROM v3_worker_outputs o "
        "JOIN v3_dispatches d ON d.id = o.dispatch_id "
        "WHERE d.task_id = ? "
        "ORDER BY o.emitted_at ASC",
        (task_id,),
    )
    return _rows(cur)


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def create_aggregation(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    synthesis_text: str,
    decision: str,
    synthesis_json: dict[str, Any] | None = None,
    refine_directive: str | None = None,
    actor: str | None = None,
) -> int:
    """Record an orchestrator's synthesis + decision for a task."""
    if decision not in ("done", "refine", "escalate"):
        raise ValueError(f"decision {decision!r} not in ('done','refine','escalate')")

    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task {task_id!r} not found")

    now = _now()
    cur = conn.execute(
        "INSERT INTO v3_aggregations "
        "(task_id, synthesis_text, synthesis_json, decision, refine_directive, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            task_id, synthesis_text,
            json.dumps(synthesis_json) if synthesis_json else None,
            decision, refine_directive, now,
        ),
    )
    agg_id = int(cur.lastrowid)

    # Drive task status off the decision
    if decision == "done":
        update_task_status(conn, task_id, "awaiting_approval", actor=actor)
    elif decision == "refine":
        update_task_status(conn, task_id, "dispatched", actor=actor)  # back to active dispatch
    elif decision == "escalate":
        update_task_status(conn, task_id, "escalated", actor=actor)

    log_event(
        conn, task_id=task_id, event_type="aggregation_created",
        actor=actor or task["orchestrator_id"],
        payload={"aggregation_id": agg_id, "decision": decision},
    )
    conn.commit()
    return agg_id


def list_aggregations(
    conn: sqlite3.Connection, task_id: str,
) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM v3_aggregations WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    )
    return _rows(cur)


# ---------------------------------------------------------------------------
# Task events (audit log)
# ---------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    actor: str | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> int:
    """Append an event to the audit log. Caller is responsible for the commit
    if they're batching, but a standalone call commits."""
    cur = conn.execute(
        "INSERT INTO v3_task_events (task_id, event_type, actor, payload_json, occurred_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            task_id, event_type, actor,
            json.dumps(payload) if payload else None,
            occurred_at or _now(),
        ),
    )
    return int(cur.lastrowid)


def list_events(
    conn: sqlite3.Connection, task_id: str,
    *, limit: int = 500,
) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM v3_task_events WHERE task_id = ? "
        "ORDER BY occurred_at ASC, id ASC LIMIT ?",
        (task_id, int(limit)),
    )
    return _rows(cur)


__all__ = [
    "init_v3_schema",
    "create_task", "get_task", "list_tasks",
    "update_task_status", "finalize_task",
    "create_dispatch", "get_dispatch", "list_dispatches",
    "update_dispatch_status",
    "record_worker_output", "get_outputs_for_task",
    "create_aggregation", "list_aggregations",
    "log_event", "list_events",
]
