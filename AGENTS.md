# AGENTS.md
# Version: 2026-03-29

**Delphi Broker — CLI Agent Execution Contract**

---

## Scope

This file applies to agents executing code locally inside the Delphi Broker repository via a CLI interface (Codex CLI, Claude Code, Gemini CLI).

Its purpose is to ensure that local, autonomous code execution is correct, disciplined, and aligned with the project's established architecture.

---

## Applicability & Precedence

Rule precedence:
1. System/developer/runtime instructions
2. `AGENTS.md`
3. Supplemental guides in `docs/` (if any)

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

### Data
- SQLite database is SSOT for messages and agent state
- WAL mode enabled; row factory returns dicts
- Message lifecycle: `PENDING -> APPROVED/REJECTED -> ACKED`
- Channels are implicit (created on first message)

### Authority
- Orchestrator role required for approve/reject/broadcast operations
- Role check via `is_orchestrator()` — no bypass, no ambient escalation
- Web UI agent (`web-ui`) is always orchestrator
- Agent self-identification is mandatory on every operation

### Configuration SSOT
- `.env` — infrastructure config (host, port, db path)
- `config/agents.json` — seed agent registry
- `config.py` — single import point; loads from both sources

### API Surface
- REST API at `/api/v1/` — programmatic access
- MCP server at `/mcp` — Claude Code / MCP client integration
- Web UI at `/web/` — phone-friendly approval interface
- Static files at `/static/`

---

## Service Boundary Contracts

- Defaults and fallbacks must be explicitly owned by the function that uses them
- Shared, implicit, or ambient fallback behavior is forbidden
- API errors use HTTP status codes (403 for unauthorized, 404 for not found)
- MCP errors return `{"error": "..."}` dicts (no exceptions across MCP boundary)

---

## Failure Semantics

### Contract violations -> Fail-loud
- Missing required fields, invalid types, malformed input
- Return appropriate HTTP error or error dict
- Do not auto-correct silently

### Dependency failures -> Observable degradation
- Database unavailable: fail-loud (no silent empty results)
- Agent not found on approve/reject: explicit 403/error

### Client input errors -> Client-classified
- Invalid UUIDs, malformed payloads: 4xx with clear message

---

## Testing Requirements

- Required for all behavioral changes
- Must be deterministic
- Tests use a separate in-memory or temp-file database, never the production DB
- Freeze volatile values (timestamps, UUIDs) or assert structural invariants

---

## What CLI Agents Must Not Do

- Introduce new dependencies without explicit instruction
- Re-architect locked components
- Refactor unrelated code for "cleanup"
- Silence errors to make tests pass
- Add heuristics, whitelists, or regex routing
- Modify `.env` or `config/agents.json` without explicit instruction
- Push to remote without explicit instruction

---

## Summary Requirement

After completing a task, CLI agents must provide:
- What root cause was identified
- How it was addressed
- What behavior changed (and what did not)
- Any remaining known limitations or deferrals
