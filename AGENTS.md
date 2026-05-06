# AGENTS.md
# Version: 2026-04-25

**Agent Broker — CLI Agent Execution Contract**

---

## Scope

This file applies to agents executing code locally inside the Agent Broker repository via a CLI interface (Codex CLI, Claude Code, Gemini CLI). Agent Broker is the parent service; Delphi consensus and v3 task dispatch are current capability surfaces inside it.

Its purpose is to ensure that local, autonomous code execution is correct, disciplined, and aligned with the project's established architecture.

The authoritative architecture contract is [`DESIGN.md`](./DESIGN.md). When this file and `DESIGN.md` disagree, `DESIGN.md` wins; raise the discrepancy with the operator before writing code.

---

## Applicability & Precedence

Rule precedence:
1. System/developer/runtime instructions
2. `DESIGN.md` (architecture contract)
3. `AGENTS.md` (this file)
4. Supplemental guides in `docs/` (if any)

---

## Core Operating Principles

1. **Correctness over superficial success**
2. **Root-cause resolution, not symptom suppression**
3. **Fail-loud, deterministic behavior**
4. **No heuristics, no whitelists, no regex routing**
5. **Generic over specific** — prefer capability-contract architectures over branches keyed to named current forms
6. **Antifragility** — failures should increase diagnosability, not inflate formalism

---

## Architectural Axioms

1. **Truth is absolute.** The database is SSOT. Configuration files are SSOT for their domain. No ambient defaults.
2. **Cognition is adaptive.** Inside bounded contracts, LLM intelligence replaces brittle rules.
3. **Antifragility over rigidity.** Failures increase diagnosability and expand capability.
4. **Structure where required, flexibility where beneficial.** Contracts govern boundaries; internals evolve freely.
5. **Fail-loud, never fail-silent.** No silent defaults, no swallowed errors, no "reasonable" fallbacks that hide problems.

---

## Locked Architectural Invariants (Do Not Change)

`DESIGN.md` is authoritative for the data model, state machine, and API surface. Items below are the invariants that may not drift even when implementation details evolve.

### Data
- SQLite database is SSOT for all session/round/iteration/review state.
- WAL mode enabled; row factory returns dicts.
- Schema is `sessions → rounds → iterations` plus `reviews` (round 3 only) and `agents`. See `DESIGN.md` §3. The v1 `messages` and `message_receipts` tables do not exist.
- Each `agent_id` has exactly one `role` from `{worker, arbitrator, executor}`. The executor cannot share an `agent_id` with a worker.
- Session state machine and iteration state machine are defined in `DESIGN.md` §4. New states are not added without updating that document first.

### Authority
- Operator authority is bound to the `DELPHI_OPERATOR_TOKEN` secret. There is no per-message authority gating.
- Agents authenticate every MCP call with HMAC-SHA256 over the per-action canonical field set; the broker enforces a 5-minute replay window on `client_ts`. There is no bypass and no ambient escalation.
- Agent self-identification is mandatory on every MCP call.

### Configuration SSOT
- `.env` — infrastructure config (operator token, host, port, db path, role overrides, web cookie security).
- `config/agents.json` — public agent manifest (`agent_id`, `host`, `role`).
- `config/agents-secrets.json` — gitignored sidecar for per-agent HMAC secrets in production.
- `src/agent_broker/config.py` — single import point; loads from all of the above.

### API Surface
- REST API at `/api/v1/session/*` — operator-facing session lifecycle.
- MCP server at `/mcp` — agent-facing tools: `delphi_poll_inbox`, `delphi_emit_response`, `delphi_emit_review`, `delphi_executor_emit`.
- Web UI at `/web/` — phone-friendly operator interface.
- Static files at `/static/`.

The v1 surfaces (`submit_message`, `approve_message`, `reject_message`, `ack_message`, `list_messages`, `get_message`, `broadcast_message`, the approval-gated message lifecycle, HTTP Basic auth on the web UI, the synthetic `web-ui` agent identity) are **deleted**. Do not reintroduce them.

---

## Service Boundary Contracts

- Defaults and fallbacks must be explicitly owned by the function that uses them.
- Shared, implicit, or ambient fallback behavior is forbidden.
- API errors use HTTP status codes (401 for unauthenticated operator, 403 for unauthorized, 404 for not found, 4xx for client input errors).
- MCP errors return `{"error": "..."}` dicts. Exceptions never cross the MCP boundary; the broker catches and translates.
- **Agents communicate via MCP tools, not direct DB writes.** Workflow advancement always goes through `workflow.py` functions; routes (`routes/api.py`) and the MCP server (`mcp_server.py`) never write workflow-mutating state to the DB directly.
- HMAC signature canonical field sets are defined in `database.py` (`build_emit_response_signature_fields`, `build_emit_review_signature_fields`, `build_executor_emit_signature_fields`, plus the `poll_inbox|agent_id|client_ts` triplet). Changing them is a breaking protocol change — update agent-side helpers in lockstep.

---

## Failure Semantics

### Contract violations → Fail-loud
- Missing required fields, invalid types, malformed input.
- Off-script agent output (response missing `output`/`self_assessment`/`rationale`, or unrecognized values) marks the iteration `off_script` and pauses the round for operator resolution. The broker does not auto-correct.
- All agent-facing failures surface as `{"error": "..."}` dicts; the broker never raises across the MCP boundary.

### Dependency failures → Observable degradation
- Database unavailable: fail-loud (no silent empty results).
- Unknown / unregistered `agent_id` on any MCP call: explicit `auth_failed` error dict.

### Workflow stalls → Pause-and-notify
- Stalled round (8 iterations without convergence, or oscillation between consecutive iterations): session moves to `escalated`; operator chooses force-converge / retry / abort.
- Cross-host outputs irreconcilable (similarity < 0.30 pre-arbitration, or arbitrator self-reports irreconcilable): session pauses for operator direction.
- Round-3 deadlock (same reviewer rejects 3+ rounds): pause; operator may skip that reviewer or abort.
- Executor failure: pause; operator may retry or abort.

The broker never advances past a pause without operator input. See `DESIGN.md` §9.

### Client input errors → Client-classified
- Invalid UUIDs, malformed payloads, expired `client_ts`, bad signature: 4xx (REST) or `auth_failed` dict (MCP) with a clear reason.

---

## Testing Requirements

- Required for all behavioral changes.
- Must be deterministic.
- Tests use a separate in-memory or temp-file database, never the production DB.
- Freeze volatile values (timestamps, UUIDs) or assert structural invariants.
- The session/round/iteration state machine is exercised in `tests/test_workflow.py`; REST surface in `tests/test_api.py`; MCP tools in `tests/test_mcp.py`; DAO and signature helpers in `tests/test_database.py`. Behavioral changes to those layers must come with matching test changes.

---

## What CLI Agents Must Not Do

- Introduce new dependencies without explicit instruction
- Re-architect locked components or alter the state machine without first updating `DESIGN.md`
- Refactor unrelated code for "cleanup"
- Silence errors to make tests pass
- Add heuristics, whitelists, or regex routing
- Modify `.env`, `config/agents.json`, or `config/agents-secrets.json` without explicit instruction
- Push to remote without explicit instruction
- Reintroduce v1 surfaces (message lifecycle, approval routing, HTTP Basic auth, deprecated MCP tools)

---

## Summary Requirement

After completing a task, CLI agents must provide:
- What root cause was identified
- How it was addressed
- What behavior changed (and what did not)
- Any remaining known limitations or deferrals
