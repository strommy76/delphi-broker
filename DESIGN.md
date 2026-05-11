# Agent Broker — v2 Delphi Consensus Architecture

**Status:** Authoritative design contract. Implementation must conform.
**Replaces:** v1 approval-gated message routing.
**Last updated:** 2026-04-25

This document is the single source of truth for the v2 Delphi consensus workflow hosted inside Agent Broker. Code that disagrees with this doc is wrong; doc that disagrees with operator intent is wrong. Resolve disagreements by escalating to the operator before writing code.

---

## 1. Purpose

Mechanize the manual copy-paste in the operator's hierarchical iterative refinement workflow. Preserve every human nudge point. Remove every mechanical text-shuffle.

The operator's current workflow:

1. Each agent on a host produces a draft prompt
2. One agent's draft goes to the other agent on the same host for synthesis
3. Synthesized prompt goes back for evaluation
4. Iterate until the same-host pair converges
5. Same process on every other host
6. Cross-host synthesis by an arbitrator agent
7. Arbitrator output goes back to all hosts for final review
8. Repeat until cross-host consensus
9. Hand to executor

This is qualitatively different from fan-out Delphi: the artifact is a *prompt*, the same-host pair is intentionally interactive (not isolated), and the diversity dimension is host-pair (model-family + physical context) rather than per-agent.

Past-session review is **not** a goal. Cross-agent context sharing is **not** a goal. Authority gating per message is **not** a goal.

---

## 2. Roles

| Role | Identity | Behavior |
|---|---|---|
| **Operator** | Bryan, via phone or laptop | Submits problems, optionally nudges at transitions, approves execution. Sole human in the loop. |
| **Worker agent (host-pair member)** | Claude or Codex on a designated host | Receives draft inputs, produces refined drafts, self-reports convergence status |
| **Arbitrator agent** | Single Claude (default: `flow-claude`) | Receives converged outputs from each host, produces cross-host synthesis |
| **Executor** | Single Codex (e.g. `dev-codex-executor`) | Receives final approved prompt, executes it |

A host always has exactly two worker agents (one Claude, one Codex). Number of hosts is configurable; default deployment has two (prod, dev).

**Role constraint:** the `agents` schema enforces exactly one `role` per `agent_id` (`worker | arbitrator | executor`). The executor therefore cannot share an `agent_id` with any worker. If the operator wants the dev-host Codex to be the executor, they must register a *separate* identity (e.g. `dev-codex-executor`) reusing the same Codex CLI under a distinct agent env file. `DELPHI_EXECUTOR_AGENT_ID` defaults to `dev-codex` for back-compat, but production deployments should override.

---

## 3. Data Model

```
sessions
  id                 UUID PRIMARY KEY
  problem_text       TEXT NOT NULL
  status             ENUM(drafting, round_1, round_2, round_3, executing, complete, aborted, escalated)
  nudge_window_secs  INTEGER NOT NULL DEFAULT 60
  created_at         TIMESTAMPTZ
  updated_at         TIMESTAMPTZ
  finalized_prompt   TEXT NULLABLE  -- populated when round 3 reaches consensus

rounds
  id            UUID PRIMARY KEY
  session_id    UUID FK
  round_num     INTEGER (1..N)
  round_type    ENUM(same_host_pair, cross_host_arbitration, multi_agent_review, execute)
  host          TEXT NULLABLE  -- e.g. 'prod' or 'dev' for round_1; NULL for rounds 2/3/exec
  status        ENUM(pending, in_progress, converged, escalated, complete, aborted)
  started_at    TIMESTAMPTZ
  ended_at      TIMESTAMPTZ NULLABLE
  outcome_text  TEXT NULLABLE  -- the converged prompt for round_1; FLOW_SYNTHESIS for round_2; null otherwise

iterations
  id                       UUID PRIMARY KEY
  round_id                 UUID FK
  iter_num                 INTEGER (1..N within round)
  source_agent             TEXT NULLABLE   -- null on the very first iter of round 1 (operator is the source)
  destination_agent        TEXT
  source_output            TEXT NOT NULL   -- text being delivered to the destination
  nudge_text               TEXT NULLABLE   -- operator's optional comment
  nudge_window_closes_at   TIMESTAMPTZ
  destination_output       TEXT NULLABLE   -- destination's response when emitted
  destination_self_assess  ENUM(converged, more_work_needed) NULLABLE
  destination_rationale    TEXT NULLABLE
  source_emitted_at        TIMESTAMPTZ
  destination_received_at  TIMESTAMPTZ NULLABLE
  destination_emitted_at   TIMESTAMPTZ NULLABLE
  status                   ENUM(awaiting_nudge, awaiting_destination, complete, off_script)

reviews
  id              UUID PRIMARY KEY
  round_id        UUID FK   -- always a round_3 round
  reviewer_agent  TEXT
  decision        ENUM(approve, reject)
  comments        TEXT NULLABLE
  rationale       TEXT NULLABLE
  emitted_at      TIMESTAMPTZ

agents (preserved from v1, simplified)
  agent_id    TEXT PRIMARY KEY
  host        TEXT
  role        ENUM(worker, arbitrator, executor)
  hmac_secret TEXT  -- (existing convention)
  ...
```

The `messages` and `message_receipts` tables are dropped. The PENDING/APPROVED/REJECTED/ACKED message lifecycle is gone.

---

## 4. State Machine

### Session-level

```
DRAFTING
  └→ ROUND_1  (broker spawns a same-host iteration round per host, in parallel)
       ├→ all rounds CONVERGED  →  ROUND_2
       └→ any round STALLED     →  ESCALATED (operator decides: force-converge, retry, abort)

ROUND_2  (single arbitrator round)
  ├→ COMPLETE   →  ROUND_3
  └→ irreconcilable  →  ESCALATED

ROUND_3  (parallel reviews from all worker agents)
  ├→ all APPROVE      →  EXECUTING (handed to executor)
  └→ any REJECT       →  MEDIATION
       ├→ ≤2 mediation attempts → re-run ROUND_2 with rejection comments → ROUND_3 again
       └→ >2 attempts          →  ROUND_1 (full restart with all accumulated comments)

EXECUTING
  ├→ executor success  →  COMPLETE
  └→ executor failure  →  ESCALATED

ABORTED  (operator-initiated, reachable from any state)
```

### Iteration-level (within a round)

```
SOURCE_EMITTED → AWAITING_NUDGE
  └→ nudge_window_closes_at reached, OR operator submits/skips
     → AWAITING_DESTINATION
        └→ destination emits structured response
           → COMPLETE (next iteration spawned, OR round converges)
        └→ destination emits malformed response
           → OFF_SCRIPT (round paused, escalation)
```

---

## 5. Convergence — Hybrid Rule

Round 1 same-host pair is **converged** when both:

1. Text similarity ≥ 0.95 between the most recent two destination outputs (using `difflib.SequenceMatcher.ratio()` on whitespace-normalized text)
2. The destination agent self-reports `CONVERGED`

If only one condition holds, iteration continues.

### Stall conditions (any one triggers ESCALATED)

- 8 iterations without convergence
- Two consecutive iterations have similarity < 0.50 (oscillation)
- Any agent emits a malformed response (missing required structured fields)

---

## 6. Agent Output Contract

Every worker/arbitrator response **must** be structured JSON:

```json
{
  "output": "<the refined prompt or synthesis>",
  "self_assessment": "CONVERGED | MORE_WORK_NEEDED",
  "rationale": "<why CONVERGED or what still needs work>"
}
```

Reviewer responses (round 3 only):

```json
{
  "decision": "APPROVE | REJECT",
  "comments": "<required if REJECT, optional if APPROVE>",
  "rationale": "<reasoning>"
}
```

Malformed responses trigger the OFF_SCRIPT failure mode. The broker does not auto-correct.

---

## 7. Nudge Mechanism

Every iteration generates a `pending_transition` event. The broker fires a phone-friendly notification:

> "**<source_agent>** finished. **<destination_agent>** is up next. Comment?"

Operator interaction (web UI, phone-friendly):

- Tap notification → see source's output (collapsible) + nudge text field + two buttons: `Submit & Continue` | `Skip`
- Default action if no interaction within `nudge_window_secs` (default 60): skip
- If submitted, nudge is prepended to the destination agent's prompt:

```
[Operator nudge — weight as guidance]: <nudge_text>

[Original input from <source_agent>]: <source_output>
```

The first iteration of round 1 has no source agent — its source is the operator's `problem_text`, and there is no nudge window (the operator already submitted everything by creating the session).

---

## 8. Round 3 Rejection — Mediated Micro-Iteration

When any round-3 reviewer rejects:

1. Broker collects all rejection comments
2. Broker invokes the arbitrator with `(prior FLOW_SYNTHESIS, [reject_comments…])`
3. Arbitrator emits `FLOW_SYNTHESIS_v2` (same output contract)
4. Round 3 repeats — all four reviewers evaluate the new synthesis
5. If approved → executing
6. If still rejected after **2** mediated attempts → full Round 1 restart with all comments accumulated as additional context

---

## 9. Failure Modes — Pause-and-Notify

Every failure is the same pattern: **broker pauses the affected session, fires a notification, and waits indefinitely for operator direction**. The broker never advances past a pause without operator input.

| Failure | Detection | Operator options |
|---|---|---|
| Round 1 stalled | 8 iters without convergence OR oscillation detected | Force-converge (use latest output) / Retry with nudge / Abort |
| Cross-host irreconcilable (pre-Flow heuristic) | text similarity between host outcomes < 0.30 | Proceed to Flow anyway / Abort / Restart Round 1 |
| Flow self-reports irreconcilable | Arbitrator emits `{"status": "irreconcilable"}` | Abort / Restart Round 1 with manual reframe |
| Round 3 deadlock | Same reviewer rejects 3+ rounds in a row | Skip that reviewer / Abort |
| Off-script agent output | Response missing required fields | Re-prompt with format reminder / Skip agent / Abort |
| Executor failure | Executor returns error / non-zero | Show error; operator decides retry vs abort |

---

## 10. Concurrency Model

- **Round 1 across hosts**: prod and dev run their iteration loops in parallel. Completely isolated; no shared state, no cross-poll.
- **Round 1 within a host**: serial. Each iteration completes before the next begins. Same-host agents see each other's outputs by design.
- **Round 2 (arbitration)**: serial, single arbitrator agent, sees only the converged outputs.
- **Round 3 (multi-agent review)**: parallel across all worker agents. Reviewers do **not** see each other's reviews.
- **Mediation**: serial (arbitrator only).

---

## 11. API Surface

### Public REST (operator)

```
POST   /api/v1/session
       body: { problem_text, nudge_window_secs?, host_pairs?, arbitrator?, executor? }
       returns: { session_id, status }

GET    /api/v1/session/{id}
       returns: full session state — current round, status, outcome (if any)

GET    /api/v1/session/{id}/pending
       returns: { transition? }   -- the iteration awaiting nudge, or null

POST   /api/v1/session/{id}/nudge
       body: { iteration_id, action: "submit" | "skip", nudge_text? }
       returns: { ok: true }

POST   /api/v1/session/{id}/abort
       returns: { ok: true, status: "aborted" }

POST   /api/v1/session/{id}/escalation/resolve
       body: { iteration_id?, action: "force_converge" | "retry" | "abort" | "skip_agent" | ... }
       returns: { ok: true, new_status }

GET    /api/v1/session/{id}/transcript
       returns: full ordered transcript (rounds → iterations → outputs/reviews)

POST   /api/v1/session/{id}/approve_execution
       returns: { ok: true, status: "executing" }
```

### Agent MCP tools

Each worker/arbitrator/executor host gets these tools via the MCP server:

```
delphi_poll_inbox()
  → returns null OR { session_id, round_num, role, input_text, request_id }
  Agent calls this regularly. If returned non-null, agent must respond before
  the broker advances.

delphi_emit_response(request_id, output, self_assessment, rationale)
  Worker/arbitrator submits their structured response.

delphi_emit_review(request_id, decision, comments?, rationale?)
  Round-3 reviewer submits APPROVE or REJECT.

delphi_executor_emit(request_id, success, output, error?)
  Executor submits the result of executing the final prompt.
```

Authentication: HMAC-SHA256 over a canonical field set per call type, using each agent's pre-shared secret. Replay protection via `client_ts` freshness window (5 minutes). All preserved from v1 (rebuild signature builders for new actions).

### Web UI surface (phone-friendly)

- `/web/` — session list (active + recent)
- `/web/session/{id}` — current state, pending transition (if any), one-tap nudge UI
- `/web/session/{id}/transcript` — full transcript (collapsible by round)
- `/web/session/{id}/escalation` — escalation resolution UI when paused

---

## 12. What's Deleted From v1

- `PENDING / APPROVED / REJECTED / ACKED` lifecycle and all surrounding logic
- `messages` and `message_receipts` tables
- Approve / reject / broadcast REST endpoints
- Per-message authority/orchestrator role checks (replaced by session ownership)
- HTTP basic auth on web UI (replaced by single session-creator token)
- `web-ui` synthetic agent identity
- Implicit channel concept (sessions replace channels for grouping)
- Existing MCP tools `submit_message`, `approve_message`, `reject_message`, `ack_message`, `list_messages`, `get_message`, `broadcast_message`

## 13. What's Preserved From v1

- FastAPI + SQLite + WAL mode (data layer technology)
- HMAC-SHA256 signing of agent communications (action/auth pattern)
- Replay-window freshness check on `client_ts`
- Per-agent env file pattern (`DELPHI_AGENT_ID`, `DELPHI_AGENT_SECRET`, `DELPHI_BROKER_URL`)
- Phone-friendly web UI shell + `style.css`
- MCP server scaffolding (URL endpoint, JSON-RPC framing)
- Tailscale-IP routing assumption (broker host + remote agent hosts)
- `config/agents.json` agent registry pattern (with role/host fields restructured)

---

## 14. Implementation Order

The implementation must land in atomic commits in this order:

1. **DESIGN.md** (this document)
2. **Database schema rewrite**: new tables, drop old, migration is destructive (no v1 data preservation)
3. **Domain models + DAO operations**: session/round/iteration CRUD
4. **Workflow engine**: convergence detection + state-machine controllers (per round type)
5. **HMAC builders for new actions**
6. **REST API**: session-centric endpoints
7. **MCP tools**: poll_inbox, emit_response, emit_review, executor_emit
8. **Web UI**: session view, nudge view, transcript view, escalation view
9. **Tests**: unit + state-machine + signature
10. **README + AGENTS.md + BOOTSTRAP.md** updates for new agent contract

Each commit must leave the system in a buildable state and pass the tests that exist at that commit. Deletion of v1 surfaces happens in commits where new replacements land, not before.

---

## 15. Open Questions For Operator

These remain operator-only decisions and must be confirmed before related code is finalized:

- **Push notification delivery**: start with web-UI polling (every 5s) on the phone; upgrade to Web Push API in a follow-up?
- **Default `nudge_window_secs`**: 60s confirmed?
- **Agent inbox poll interval**: 2s? 5s? Configurable?
- **Maximum total session lifetime** before auto-abort: 24h? 72h?
- **Operator authentication**: cookie-based session token tied to a single secret in `.env`, or external auth?

Defaults proposed by the contract (60s nudge, polling UI, 5s agent poll, 24h max lifetime, env-secret cookie auth) will ship unless operator overrides.

---

## 16. Out Of Scope For This Refactor

- Past-session search/recall (the operator does not need this)
- Cross-session learning or session deduplication
- Multi-operator support (single operator assumed)
- Lexx integration (will come separately when Lexx is operational)
- Cross-agent context sharing (intentionally forbidden — destroys Delphi diversity)

---

## 17. Operator-Mediated Collaboration Domain

Issue #11 adds a separate collaboration domain alongside the v2 Delphi
workflow. This domain exists to replace manual operator copy/paste between
Lexx agents while preserving an explicit operator review point before one
agent's draft can be delivered to another agent.

This section does not alter the Delphi session/round/iteration/review state
machine. The deleted v1 approval-gated message lifecycle remains deleted. The
collaboration domain uses new domain names and new persistence surfaces.

### Authority Rule

Operator approval is load-bearing server authority. No agent-authored
collaboration draft is deliverable to a recipient participant until an
operator decision approves, edits-and-approves, or redirects-and-approves it.

The approval check is enforced in the collaboration delivery authority and by
store-layer fail-loud guards where the invariant is representable in SQLite.
MCP, HTTP, web, and helper adapters are projections over that authority; they
do not own delivery decisions.

### Relationship To Peer Messaging

The current `peer_*` domain remains immediate peer-to-peer messaging. It is
used by Pi participants today and must not degrade.

The new collaboration domain uses a separate `collab_*` namespace. `peer_*`
tables are not mutated for the collaboration MVP. `peer_messages`,
`peer_receipts`, and `peer_events` keep their existing schema and trigger
semantics.

The `collaboration_governed` participant property scopes only to the
`collab_*` collaboration substrate. Delphi v2/v3 workflows retain their
existing orchestrator-role authorization model unchanged.

The bypass boundary is participant-level. Participant configuration marks a
participant as governed by operator-mediated collaboration with an explicit
property. Only collaboration-governed participants may use collaboration tools.
A collaboration-governed participant must use the collaboration tools for
outbound collaboration messages; direct `peer_send` delivery for that governed
participant fails loud. Participants that are not collaboration governed keep
direct peer behavior and are not collaboration participants. This is a
property check, not a branch on agent names, host names, workflow names,
transport routes, or deployment topology.

Per-message governance overrides are out of scope for the MVP. They can be
introduced later only with explicit conflict-resolution rules between
participant policy and message policy.

### Paradigm Direction

The two-lane shape (peer for non-governed, collab for governed) is a
transitional accommodation, not the target paradigm. The operator-acknowledged
collaboration substrate is the canonical channel for all agent-to-agent
communication. The peer lane survives only to preserve existing non-governed
Pi participant traffic during migration.

Target end state: every agent participant carries `collaboration_governed:
true`, and `collab_propose_message` is the single agent-to-agent send path.
Routine ops messages (status pings, acks, log relay, calibration coordination)
flow through the same operator approval as substantive collaboration messages;
the operator-acknowledgment friction is light by design and falls to a single
approve action for routine traffic.

Rationale for universal coverage rather than per-traffic-class lanes: the
prior non-governed peer model required operator attention informally after
side effects had fired (read the agent's summary, course-correct downstream,
relay to recipient). Moving the operator gate upstream of the send is
net-equivalent or net-positive on operator wall-clock, because corrected
messages no longer waste a downstream round. The audit trail and the
single-channel discipline are additional gains.

`peer_*` for agent-to-agent traffic is legacy in transition. It retires once
all participants migrate to collaboration governance. The property-scoped
guard at the peer delivery authority preserves the no-bypass invariant during
the migration window. After migration, the guard and the peer agent-to-agent
path retire together as phantom code.

Pi participant migration is sequenced after current ship-train completion to
avoid mid-flight substrate retrofit. The agents.json flip from
`collaboration_governed: false` to `true` is the entire migration mechanism
for participants whose code already uses the broker via signed canonical
fields; Pi-Claude and Pi-Codex existing call sites swap `peer_send` for
`collab_propose_message` without identity reissue.

### Persistence Classification

SQLite remains the SSOT for broker communication state.

Canonical / authoritative collaboration state:

- `collab_threads` or equivalent thread grouping
- `collab_drafts`
- `collab_draft_recipients`
- `collab_operator_decisions`
- `collab_decision_recipients`
- `collab_deliverables`
- `collab_receipts`

Observational collaboration state:

- `collab_events` append-only audit events

Derived projections:

- pending operator queue
- participant inbox
- thread transcript views
- audit detail views

Derived projections are query/read-model surfaces. They must not become
independent persistence authorities.

### Collaboration Lifecycle

```
draft_created
  -> pending_operator_decision
     -> approved
     -> edited_and_approved
     -> redirected_and_approved
     -> rejected

approved / edited_and_approved / redirected_and_approved
  -> deliverable
  -> delivered
  -> acked
```

The operator decision preserves the original draft, the decision record, the
final deliverable form when applicable, and correlation across all audit
events. Rejected drafts never become deliverable.

Recipient visibility is approval-gated. A recipient calling the collaboration
thread view before approval does not see draft bodies addressed to them.
Recipient-visible thread content begins only from approved deliverables visible
to that recipient. The drafting participant can see its own drafts and
decision state. The operator can see all drafts, decisions, deliverables,
receipts, and audit events.

### Idempotency

Draft submission idempotency is keyed by `(from_participant, correlation_id)`.
A retry with the same participant, same correlation id, and same canonical
draft payload returns the existing draft id. A retry with the same participant
and correlation id but different canonical payload fails loud as an idempotency
conflict.

Approval, delivery, and ack operations are also idempotent:

- A repeated identical approval decision returns the existing decision.
- A second delivery materialization for the same approved decision returns the
  existing deliverable.
- A repeated ack by the authorized recipient reports the existing ack state.
- An ack by a non-recipient fails loud.

### Store-Layer Guards

The collaboration store enforces invariants below, either with SQLite
constraints/triggers or with a store helper plus tests when a trigger cannot
express the condition cleanly:

- Draft rows are immutable after creation except for state transitions owned
  by collaboration store helpers.
- Draft recipient rows and decision recipient rows are immutable after
  creation. Their recipient sets close after the corresponding draft-created
  or operator-decision audit event.
- Operator decisions are append-only and reference an existing draft.
- Deliverables reference an approved / edited-and-approved /
  redirected-and-approved decision. A deliverable without approval evidence is
  rejected. Deliverable sender, content, payload, correlation, and thread
  identity must match the authorized decision/draft form. Deliverable creation
  requires the corresponding operator-decision audit event.
- Receipts reference deliverables, not drafts. A receipt cannot exist for an
  unapproved draft, for a participant outside the decision-recipient set, or
  with recipient metadata/order that differs from the approved decision
  recipient row. A receipt cannot make a deliverable pollable until the
  `deliverable_created` audit event exists.
- Delivered and acked timestamps are write-once.
- Audit events are append-only.

Adapters and services do not write raw SQL for collaboration state outside the
store seam.

### Agent And Operator Surfaces

Agent-facing MCP tools are generic collaboration primitives:

- propose a message draft
- poll approved deliverables
- acknowledge a deliverable
- get a visible thread transcript

Each tool keeps HMAC-SHA256 authentication and the 5-minute replay window.
Canonical signature fields are documented with the tool definitions and must
be mirrored by any client helper. A helper signs canonical fields that the
broker independently verifies; it does not approve, deliver, or short-circuit
broker decisions.

Operator HTTP/web surfaces cover:

- pending draft queue
- approve as-is
- edit and approve
- redirect and approve
- reject
- thread transcript
- probe visibility toggle
- audit event detail

Operator authority remains bound to `DELPHI_OPERATOR_TOKEN`; no new auth model
is introduced.

### Delivery Model

The MVP delivery model is polling. Agents poll for approved deliverables and
ack them after receipt. Push notifications, SSE, webhooks, and streaming
delivery are out of scope for the MVP because they introduce additional
delivery-capable surfaces.

### Probe Segregation

Probe/default-hidden behavior derives from participant metadata such as
`is_probe`. The collaboration domain must not use a hardcoded hidden-thread
sidecar list as its visibility authority. Probe traffic is hidden from default
operator views and visible only through an explicit include-probes request.

### Required Regression Tests

The implementation must add durable tests proving:

- A collaboration-governed participant cannot deliver through `peer_send`.
- Existing direct peer Pi behavior still supports `send -> poll -> ack ->
  get_thread`.
- Recipient thread reads do not expose pending draft bodies before approval.
- Duplicate draft submission is idempotent by `(from_participant,
  correlation_id)` and conflicting payload reuse fails loud.
- Store-level or store-seam guards reject deliverable / receipt state without
  approval evidence.
- Generic collaboration core does not branch on deployment identity
  categories such as participant identity, host identity, model identity,
  provider identity, workflow form, or transport route.

---

## 18. Reviewer Notes

This contract is intended for review by an independent agent on a different host before implementation lands. Reviewer should evaluate:

- Whether the state machine covers all paths the operator's verbal workflow implies
- Whether the convergence rule is sufficient (text similarity + self-assessment AND-gate)
- Whether the failure modes cover the realistic break paths
- Whether the API surface matches the operator's phone-first interaction pattern
- Whether anything important is missing or under-specified

If the reviewer flags substantive issues, the contract is updated *before* implementation continues.

---

*End of design contract.*
