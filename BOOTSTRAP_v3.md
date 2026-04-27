# Delphi Broker — v3 Bootstrap

> **For the operator + agents who already completed v2 bootstrap**.
> v3 reuses everything from `BOOTSTRAP.md` Phase 1 (agent identities,
> HMAC secrets, MCP wiring). This guide only covers the v3 deltas.

---

## Operator pre-flight (already done if v2 is running)

If the broker is running with the v2 agents.json + agents-secrets.json
populated, you're already set up. The same broker serves both v2 and v3
endpoints; the same agent identities work.

If you're starting from scratch, see `BOOTSTRAP.md` for v2 Phase 1.

---

## v3 has six new MCP tools

The broker auto-registers them on startup. Agents see them appear in
their MCP client's tool list once they reconnect (or via
`notifications/tools/list_changed` — most clients refresh
automatically).

### Orchestrator tools

| Tool | When you call it |
|---|---|
| `delphi_v3_get_pending_task` | Each cycle, to discover tasks where you're the orchestrator |
| `delphi_v3_dispatch` | After deciding what subtask each worker gets |
| `delphi_v3_collect_outputs` | When you're ready to synthesize (workers have responded) |
| `delphi_v3_aggregate` | Submit your synthesis + decision (done / refine / escalate) |

### Worker tools

| Tool | When you call it |
|---|---|
| `delphi_v3_poll_dispatches` | Each cycle, to discover subtasks dispatched to you |
| `delphi_v3_emit_output` | When you've finished a subtask |

---

## Signature canonicals (v3 deltas)

Same pipe-separator convention as v2. HMAC-SHA256 with your agent secret.

```
v3_get_pending_task | <agent_id> | <client_ts>
v3_dispatch         | <agent_id> | <client_ts> | <task_id> | <worker_id>
v3_collect_outputs  | <agent_id> | <client_ts> | <task_id>
v3_aggregate        | <agent_id> | <client_ts> | <task_id> | <decision>
v3_poll_dispatches  | <agent_id> | <client_ts>
v3_emit_output      | <agent_id> | <client_ts> | <dispatch_id>
```

`<client_ts>` is ISO-8601 UTC, `<decision>` is one of `done`/`refine`/`escalate`.

---

## Operator workflow

### 1. Create a task

Open the web UI: `http://100.81.33.20:8420/web/v3/new`

Fill in:
- **Title** — short label
- **Orchestrator** — dropdown of registered agents. Default `pi-claude`.
  *(Per the independence rule, the chosen orchestrator is precluded from
  worker roles in this task.)*
- **Problem text** — full problem statement
- **task_json** *(optional)* — structured fields for the orchestrator

Click "Create task." You're redirected to the task view.

### 2. Wait

The task is now in the orchestrator's pending-task list. The orchestrator
polls (or, eventually, gets pushed to) and decides what to do.

You'll see status transitions live: `new → dispatched → ... → awaiting_approval`.

### 3. Review

When the orchestrator decides "done," the task lands in
`awaiting_approval`. The view shows:
- The orchestrator's synthesis
- All worker outputs (collapsible per worker)
- The full audit log

You decide:
- **Approve** — finalize with the synthesis as the artifact (you can edit
  before approving)
- **Refine** — send back to the orchestrator with a comment describing
  what needs another pass
- **Abort** — kill the task

---

## Agent loop (simplified, per-cycle pseudocode)

### Orchestrator loop

```python
while True:
    tasks = delphi_v3_get_pending_task(...)
    for task in tasks:
        if task.status == "new":
            # Decompose problem_text into subtasks
            for worker in choose_workers(task):
                delphi_v3_dispatch(task_id=task.id, worker_id=worker, subtask_text=...)
        elif task.status in ("dispatched", "aggregating"):
            outputs = delphi_v3_collect_outputs(task_id=task.id)
            if all_workers_responded(outputs):
                synthesis = synthesize(outputs)
                delphi_v3_aggregate(task_id=task.id, synthesis_text=synthesis,
                                    decision="done")
    sleep(30)
```

### Worker loop

```python
while True:
    dispatches = delphi_v3_poll_dispatches(...)
    for d in dispatches:
        if d.status == "pending":
            # Read d.subtask_text + d.task_problem_text for context
            output = produce_output(d)
            delphi_v3_emit_output(dispatch_id=d.dispatch_id, output_text=output)
    sleep(30)
```

The polling cadence is operator-tunable; 30s is sensible for development,
shorter for time-sensitive tasks.

---

## Wake-up problem (current state)

MCP doesn't natively wake idle CLI sessions. Phase-1 spike confirmed the
broker can emit `notifications/message` log events on the SSE channel,
but whether Claude Code surfaces those to the user as chat events is
empirical — pending pilot validation.

**Until we know:** orchestrator + workers run polling loops. The operator
prompts each agent CLI **once at session start** with a "stay in poll-loop
until task done" directive. After that, agents drive themselves through
all dispatches without further operator nudging.

**Once we know push works:** broker emits a notification on every
dispatch / aggregation transition, agents react in their open MCP session,
polling becomes a backstop.

See [issue #7](https://github.com/strommy76/delphi-broker/issues/7) for the
push-integration plan.

---

## Recovery / debugging

| Symptom | Diagnostic |
|---|---|
| Tool not in agent's CLI | `claude mcp list` (or Codex equivalent); verify URL is `http://100.81.33.20:8420/mcp`. Restart the CLI. |
| `auth_failed: invalid signature` | Secret mismatch. Operator regenerates and updates `config/agents-secrets.json`; restart broker. |
| `auth_failed: client_ts outside replay window` | Clock skew. Sync time on the agent's host. |
| Task stuck in `dispatched` with no progress | Workers aren't polling. Re-prompt their CLI sessions. |
| Orchestrator returns errors on aggregate | Check that all dispatches are `done` before aggregating; check decision string is exactly `done`/`refine`/`escalate`. |

---

## Useful one-liners

```bash
# List all agents (and their roles)
curl -sS -H "X-Operator-Token: $TOKEN" http://localhost:8420/api/v2/agents | jq

# Watch a task's audit log
watch -n 5 "curl -sS -H 'X-Operator-Token: $TOKEN' http://localhost:8420/api/v2/tasks/$TASK_ID/events | jq '.events[-5:]'"

# Inspect what dispatches a worker is sitting on
curl -sS -H "X-Operator-Token: $TOKEN" "http://localhost:8420/api/v2/tasks?status_filter=dispatched" | jq
```
