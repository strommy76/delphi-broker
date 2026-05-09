# Issue #11 Gate A - Operator-Mediated Collaboration Substrate

Baseline SHA: `7304fcfba8ec23a35ae4e5e2b3a7fb1f0ecd5920`

Branch: `issue-11-operator-mediated-collaboration`

Classification: `large_scope`. The work crosses persistent schema, service seams, MCP tools, operator web/API surfaces, participant authorization, no-bypass validation, and cross-host runtime proof. The MVP remains bounded to issue #11 and does not authorize a #9/#10 broker-core rewrite.

## Authority Reconciliation

`DESIGN.md` is authoritative over the current v2 Delphi workflow and explicitly says past-session review, cross-agent context sharing, and per-message authority gating are not v2 Delphi goals (`DESIGN.md:29`). It also records that the old v1 `messages` / `message_receipts` tables and `PENDING / APPROVED / REJECTED / ACKED` lifecycle are deleted (`DESIGN.md:105`, `DESIGN.md:319-320`).

Issue #11 therefore cannot reintroduce deleted v1 surfaces or mutate the Delphi state machine. The implementation must add a separate collaboration domain for operator-mediated agent communication. `DESIGN.md` needs a new section describing that domain before dependent code lands, while preserving the v2 Delphi contract.

`AGENTS.md` also locks MCP HMAC/replay semantics (`AGENTS.md:62`), operator authority through `DELPHI_OPERATOR_TOKEN` (`AGENTS.md:61`), config SSOTs (`AGENTS.md:67`), and deleted v1 surface prohibition (`AGENTS.md:77`). The collaboration domain must preserve those invariants.

## Architectural Decision

Use a new `collab_*` persistence namespace alongside the current `peer_*` tables.

Rationale:

- Reusing `peer_*` directly would force immediate-delivery peer messages to become draft-first, which conflicts with the hard constraint that existing peer-to-peer behavior must not degrade.
- The existing peer store already has useful patterns: immutable messages, append-only events, receipt guards, and atomic helper structure (`src/agent_broker/peer/peer_store.py:32`, `src/agent_broker/peer/peer_store.py:39`, `src/agent_broker/peer/peer_store.py:55`, `src/agent_broker/peer/peer_store.py:67`, `src/agent_broker/peer/peer_store.py:93-117`).
- A bounded #9/#10 broker-core slice is not required for the MVP if the new collaboration namespace is explicit, service-owned, and documented. Broader substrate consolidation remains a later decision.

The risk of parallel authority is controlled by scope: `peer_*` remains immediate peer-to-peer communication; `collab_*` is the only operator-mediated collaboration authority. A message belongs to exactly one domain at creation.

## Peer Surface Reuse

| Surface | Disposition | Reason |
|---|---|---|
| Participant identity lookup | Adapt | `IdentityService` already resolves participant properties from config (`src/agent_broker/peer/identity_service.py:32-44`), but collaboration needs an explicit participant capability/policy property for operator-mediated delivery. |
| HMAC/replay verification | Reuse | Existing MCP verification is centralized and fail-loud (`src/agent_broker/mcp_server.py:77-97`) and signature helpers live in `database.py` (`src/agent_broker/database.py:834-929`). |
| Peer store schema patterns | Adapt | SQLite trigger and atomic event patterns are the right shape, but collaboration needs draft, approval, decision, deliverable, and receipt authority separate from immediate peer delivery. |
| Peer delivery service | Leave parallel | Current `PeerDeliveryService.send()` creates a deliverable message and receipts immediately (`src/agent_broker/peer/peer_delivery_service.py:55`, `src/agent_broker/peer/peer_delivery_service.py:127`). That is correct for Pi peer-to-peer and wrong for Lexx collaboration. |
| Peer MCP tools | Leave parallel with scope guard | `peer_send`, `peer_poll`, `peer_ack`, and `peer_get_thread` are live tools (`src/agent_broker/peer/peer_mcp_tools.py:57`, `src/agent_broker/peer/peer_mcp_tools.py:139`, `src/agent_broker/peer/peer_mcp_tools.py:182`, `src/agent_broker/peer/peer_mcp_tools.py:229`). They remain for Pi peer behavior but must not bypass collaboration-governed delivery. |
| Operator transcript surfaces | Adapt | Current peer API/web read surfaces already enforce operator auth and probe visibility (`src/agent_broker/peer/peer_api.py:39-65`, `src/agent_broker/peer/peer_web.py:49-65`). Collaboration needs analogous pending-draft, decision, transcript, and audit views. |
| Probe segregation | Reuse pattern | `include_probes` is explicit and default-hidden (`src/agent_broker/peer/peer_api.py:56-65`, `tests/test_peer_operator.py:146-192`). |
| Peer boundary tests | Reuse pattern | Existing tests enforce store/service boundary discipline and probe visibility (`tests/test_peer_services.py:764-854`, `tests/test_peer_operator.py:146-192`). |

## `peer_send` Disposition

Recommended disposition: preserve `peer_send` for direct peer-to-peer participants while blocking it for collaboration-governed participants via an explicit participant property.

This is not a deployment-name branch. The implementation should add explicit collaboration governance metadata at the participant boundary. Only governed participants may use collaboration tools. Participants governed by operator-mediated collaboration must use `collab_propose_message`; direct `peer_send` delivery for that governed scope fails loud. Existing non-governed peer participants remain direct-peer capable and are not collaboration participants. Per-message governance overrides are out of scope for the MVP.

Implementation must prove:

- Direct peer `peer_send` round trip still passes for non-governed participants.
- A collaboration-governed participant cannot deliver through `peer_send`.
- The guard reads declared properties, not participant names, host names, or deployment literals.

## Collaboration Lifecycle

Proposed state model for `DESIGN.md` before code:

```text
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

The operator decision is the authority transition. Edit, reject, and redirect preserve the original draft, the decision, the final deliverable form when applicable, and correlation across all audit events.

Recipient thread visibility is approval-gated: recipients do not see pending draft bodies before approval. The sender can see its own drafts and decisions. The operator can see all draft, decision, delivery, receipt, and audit evidence.

## Service Seams

- `collaboration_identity`: resolves participants and collaboration governance properties from config.
- `collaboration_drafts`: creates pending drafts from authenticated agents.
- `collaboration_approvals`: records operator approve/edit/reject/redirect decisions.
- `collaboration_delivery`: exposes only approved deliverables to recipients and owns ack/idempotency.
- `collaboration_transcript`: read-only transcript/audit projection.
- `collaboration_store`: SQLite schema, migrations, append-only events, and store-layer guards.
- MCP and HTTP/web adapters call these seams; adapters do not own state transitions.

Names are implementation suggestions, not final API commitments. The required property is one stable concept per seam.

Persistence classification: collaboration drafts, decisions, deliverables, receipts, and thread grouping are canonical broker communication state. Collaboration audit events are observational evidence. Pending queues, inboxes, transcripts, and audit-detail views are derived projections and must not become independent persistence authorities.

## No-Bypass Authority Map

Every delivery-capable path must be blocked unless it flows through collaboration delivery authority and observes an approval record.

| Path | Required guard |
|---|---|
| `collab_poll` MCP | Returns only deliverables with approval evidence. |
| `collab_get_thread` MCP | Transcript can include drafts/decisions visible to participants, but recipient-deliverable content must follow delivery authority. |
| Operator HTTP/web | Can create approval decisions only after operator token verification; cannot bypass delivery state. |
| Client helper | Signs canonical fields only; broker independently verifies signature and state. |
| Collaboration service calls | Delivery service checks approval record and recipient authority. |
| Store layer | Fail-loud guard prevents a deliverable row/state without approval evidence where feasible. |
| Existing `peer_send` | Fails loud for collaboration-governed participants/messages; remains direct peer for non-governed peer use. |

The approval check must live at the delivery authority seam and/or as a store-layer fail-loud guard. Adapter-only enforcement is insufficient.

## MCP Tool Shape

The agent-facing surface should use generic collaboration primitives:

- `collab_propose_message`
- `collab_poll`
- `collab_ack`
- `collab_get_thread`

Canonical signature field sets must be documented with the tools and validated by tests. Client-visible discovery is required; server registration alone is insufficient.

## Operator Surface

Add operator-authenticated API/web surfaces for:

- pending draft queue
- approve as-is
- edit and approve
- redirect and approve
- reject
- thread transcript
- probe visibility toggle
- audit event detail

Operator authority stays bound to `DELPHI_OPERATOR_TOKEN`; no new auth model is in scope.

## Validation Plan

Targeted validation:

- Unit tests for draft creation, approval, edit, redirect, reject, delivery, poll, ack, transcript projection, idempotency, and restart redelivery.
- Negative tests proving unapproved delivery fails through MCP, HTTP/web, helper, service, store, and `peer_send`.
- Regression tests proving existing direct peer `send -> poll -> ack -> get_thread` behavior remains green.
- Probe segregation tests: default-hidden, explicit include-visible.
- Static/source review proving generic collaboration core does not branch on deployment identity categories: participant identity, host identity, model identity, provider identity, workflow form, or transport route.
- MCP client-visible discovery proof for collaboration tools.
- Cross-host live probe: dev proposes to prod, operator approves/edits, prod receives and acks; repeat prod to dev. If environment blocks live proof, record the blocker and obtain explicit approval for a substitute before closeout.

Full validation:

- `pytest`
- `ruff check .`
- `black --check .`

## Disproof Matrix

Construct and prove rejection or correct handling:

- Draft visible to recipient before approval.
- `peer_send` delivers a collaboration-governed message.
- Helper signs incorrectly or omits canonical field.
- Store insert creates deliverable state without approval evidence.
- Operator edit loses original draft evidence.
- Redirect delivers to original recipient.
- Reject still delivers.
- Duplicate retry creates duplicate delivery.
- Ack by non-recipient succeeds.
- Probe traffic visible in default operator view.
- Generic logic branches on current deployment identity values rather than declared properties.

## Stop Conditions

Stop and ask before implementation continues if:

- Existing peer-to-peer behavior cannot be preserved structurally.
- `peer_send` cannot be scoped without deployment-identity branching.
- `DESIGN.md` reconciliation requires redefining Delphi v2 rather than adding a separate collaboration domain.
- A no-bypass disproof requires adapter-only discipline.
- Operator authority needs a new auth model.
- HMAC/replay semantics must change.
- Generic collaboration logic needs provider/model/host/workflow branching.
- Implementation pressure crosses into Delphi/v3 migration, Lexx runtime integration, provider/model work, public exposure, dependency upgrades, or #9/#10 rewrite.

## Phase Plan

Phase 1: Gate A artifact only.

Phase 2 after acknowledgment: update `DESIGN.md` with the collaboration domain lifecycle and seams, plus any README/BOOTSTRAP notes needed for discoverability.

Phase 3: persistence/contracts/store-layer guards and unit tests. Phase 3 must confirm `peer_*` table schemas remain unchanged, add only `collab_*` persistence for this MVP, and pin store guards for deliverables/receipts without approval evidence.

Phase 4: services and no-bypass tests.

Phase 5: MCP tools, client helper, operator API/web surfaces, client-visible discovery proof.

Phase 6: cross-host dev/prod live probe, PR evidence, and closeout.

## Non-Goals

- Do not change Delphi v2/v3 workflow behavior.
- Do not integrate with Lexx runtime, ledger, memory, or artifact stores.
- Do not require Pi as a Lexx collaboration participant.
- Do not expose the broker publicly.
- Do not add provider/model-specific behavior.
- Do not upgrade dependencies without explicit re-approval.
- Do not reintroduce deleted v1 message-approval surfaces.

## Gate A Ask

Approval requested for:

- New `collab_*` namespace alongside current `peer_*`.
- Participant-property-scoped `peer_send` guard that preserves existing peer behavior while blocking collaboration-governed bypass.
- DESIGN-first implementation order before dependent code.
- Validation plan including Pi peer regression, no-bypass disproofs, client-visible MCP proof, and dev/prod cross-host live probe.
