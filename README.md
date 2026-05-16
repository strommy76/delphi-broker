# Agent Broker

Agent Broker hosts agent communication workflows behind authenticated local
services. The current surfaces are the Delphi consensus workflow, v3 task
dispatch, peer-to-peer messaging, and operator-mediated collaboration for
participants whose messages require an explicit approval gate.

The authoritative architecture spec is [`DESIGN.md`](./DESIGN.md). This README is operator-facing setup; `DESIGN.md` is the contract.

## Purpose

Replaces v1's approval-gated message routing with a session-driven iterative pipeline. A *session* moves a single problem from the operator's `problem_text` through three rounds — same-host pair refinement, cross-host arbitration, multi-agent review — and on to an executor. Convergence is detected by the broker (text similarity AND agent self-assessment). The operator nudges between iterations on a phone-friendly UI; everything else is mechanical.

See [`DESIGN.md` §1](./DESIGN.md#1-purpose) for the full motivation.

## Workflow

1. Operator submits a `problem_text` from the phone or laptop, opening a **session**.
2. **Round 1 — same-host pair.** Each host runs a serial iteration loop between its Claude and Codex worker. After every iteration the broker pauses for a short *nudge window* (default 60s); the operator can drop a one-line comment that gets prepended to the next agent's input, or skip. Iteration continues until both agents on a host converge (text similarity ≥ 0.95 *and* the destination self-reports `CONVERGED`). Hosts run in parallel; each is isolated.
3. **Round 2 — cross-host arbitration.** The arbitrator agent (`flow-claude` by default) sees only the converged outputs from each host and emits a single synthesis.
4. **Round 3 — multi-agent review.** All worker agents review the arbitrator's synthesis in parallel. Each emits `APPROVE` or `REJECT` with comments.
5. Any rejection triggers a mediated re-arbitration (up to 2 attempts); persistent rejection restarts Round 1 with all comments accumulated.
6. Once all reviewers approve, the operator confirms and the prompt is handed to the **executor**.

Failures (stalled rounds, off-script outputs, irreconcilable hosts, executor errors) all pause the session and notify the operator; the broker never advances past a pause without input. See [`DESIGN.md` §4](./DESIGN.md#4-state-machine) and [§9](./DESIGN.md#9-failure-modes--pause-and-notify).

## Architecture

- **FastAPI** application with three surfaces:
  - `/api/v1/` — REST API (operator-facing: create session, pending transition, nudge, abort, escalation, transcript, approve execution)
  - `/mcp` — MCP server exposing Delphi, peer, and collaboration tools behind HMAC verification
  - `/web/` — Phone-friendly operator UI (session list, pending nudge, transcript, escalation resolution, collaboration approvals)
- **SQLite** (WAL mode) is the single source of truth. Schema: `sessions`, `rounds`, `iterations`, `reviews`, `agents`. The v1 `messages`/`message_receipts` tables are gone.
- **Authentication.** Operator: shared `DELPHI_OPERATOR_TOKEN` (cookie-based session for web, header for REST). Agents: per-agent HMAC-SHA256 over canonical fields, with a 5-minute replay window on `client_ts`.
- **Concurrency.** Round 1 runs in parallel across hosts but serially within a host; Round 3 runs in parallel across reviewers. See [`DESIGN.md` §10](./DESIGN.md#10-concurrency-model).

## Configuration

Infrastructure config lives in `.env` (copy from `.env.example`):

```
# Required
DELPHI_OPERATOR_TOKEN=GENERATE_WITH_token_hex_32

# Network
DELPHI_HOST=0.0.0.0
DELPHI_PORT=8420
DELPHI_MCP_HOST_REGISTRY=127.0.0.1:*,localhost:*
DELPHI_MCP_ORIGIN_REGISTRY=http://127.0.0.1:8420,http://localhost:8420

# Storage
DELPHI_DB_PATH=data/delphi.db

# Agent identity
DELPHI_AGENTS_PATH=config/agents.json
OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH=config/operator_permanently_hidden_threads.json.example
OPERATOR_PARTICIPANT_ID=operator
DELPHI_ARBITRATOR_AGENT_ID=flow-claude
DELPHI_EXECUTOR_AGENT_ID=dev-codex-executor

# Web UI
DELPHI_WEB_SECURE=false   # set true when fronted by HTTPS
DELPHI_NUDGE_SWEEP_ENABLED=true
DELPHI_MCP_SESSION_MANAGER_ENABLED=true
```

| Variable | Required | Unset behavior | Notes |
|---|---|---|---|
| `DELPHI_OPERATOR_TOKEN` | yes | fail loud on protected web/API use | Generate with `python -c "import secrets; print(secrets.token_hex(32))"`; placeholders are rejected |
| `DELPHI_HOST` | yes | fail loud at startup/import | Localhost mode uses `127.0.0.1` |
| `DELPHI_PORT` | yes | fail loud at startup/import | |
| `DELPHI_MCP_HOST_REGISTRY` | yes | fail loud at startup/import | Deployment host-header registry for MCP transport security |
| `DELPHI_MCP_ORIGIN_REGISTRY` | yes | fail loud at startup/import | Deployment Origin registry for HTTP ingress |
| `DELPHI_DB_PATH` | yes | fail loud at startup/import | Resolved relative to project root unless absolute |
| `DELPHI_AGENTS_PATH` | yes | fail loud at startup/import | Public agent manifest (committed) |
| `OPERATOR_PERMANENTLY_HIDDEN_THREADS_PATH` | yes | fail loud at startup/import | Relative path to operator-managed hidden-thread config; the committed example is an empty seed |
| `OPERATOR_PARTICIPANT_ID` | yes | fail loud at startup/import | Must exist in `config/agents.json` |
| `DELPHI_ARBITRATOR_AGENT_ID` | yes | fail loud at startup/import | Must exist with `role='arbitrator'` |
| `DELPHI_EXECUTOR_AGENT_ID` | yes | fail loud at startup/import | Must exist with `role='executor'` (cannot also be a worker — see `DESIGN.md` §2) |
| `DELPHI_WEB_SECURE` | yes | fail loud at startup/import | `true`/`1`/`yes` flags the operator session cookie `Secure` |
| `DELPHI_NUDGE_SWEEP_ENABLED` | yes | fail loud at startup/import | Enables the background expired-nudge sweep |
| `DELPHI_MCP_SESSION_MANAGER_ENABLED` | yes | fail loud at startup/import | Enables the FastMCP stream session manager for HTTP MCP transport |

Agent registry lives in committed `config/agents.json`. Each agent entry declares `agent_id`, `host`, exactly one `role` from `worker | arbitrator | executor | operator`, participant metadata, and an explicit `collaboration_governed` boolean. Per-agent HMAC secrets live in `.env` as `DELPHI_AGENT_SECRET_<NORMALIZED_AGENT_ID>`. Participants that must use operator-mediated collaboration set `collaboration_governed: true`; direct peer sends involving those participants are rejected and must use the collaboration lifecycle.

## Running

### Docker (production)

```bash
cp .env.example .env  # generate DELPHI_OPERATOR_TOKEN and DELPHI_AGENT_SECRET_* values
docker compose -p agent-broker up -d --build
```

> The `-p agent-broker` flag isolates this stack from other compose projects on the same host.
> To expose the broker on a private-network interface, set `BROKER_TAILSCALE_IP`
> in `.env` and run with `-f docker-compose.yml -f docker-compose.tailscale.yml`.

Data persists in `./data/` (SQLite DB). Agent registry is mounted read-only from `./config/`.

### Local (development)

```bash
pip install -r requirements.txt
cp .env.example .env  # generate DELPHI_OPERATOR_TOKEN and DELPHI_AGENT_SECRET_* values
PYTHONPATH=src python -m agent_broker.main
```

## MCP Client Configuration

Add to `~/.claude/settings.json` on each agent host:

```json
{
  "mcpServers": {
    "agent-broker": {
      "type": "url",
      "url": "http://<broker-host>:8420/mcp"
    }
  }
}
```

See [`BOOTSTRAP.md`](./BOOTSTRAP.md) for the full agent self-configuration flow.

## Agent Contract

Agents interact with the broker through HMAC-authenticated MCP tools. Every tool requires an `agent_id`, a fresh `client_ts` (ISO 8601, within 5 minutes of broker time), and an HMAC-SHA256 `signature` over the per-action canonical field set.

| Tool | Caller | Purpose |
|---|---|---|
| `delphi_poll_inbox` | any agent | Returns pending iterations (where the agent is the destination) and pending review requests. Polled regularly by every connected agent. |
| `delphi_emit_response` | worker / arbitrator | Submits a structured response (`output` + `self_assessment` + `rationale`) for an open iteration. |
| `delphi_emit_review` | round-3 reviewer (worker) | Submits `APPROVE` or `REJECT` with optional comments and rationale. |
| `delphi_executor_emit` | executor | Reports the success/failure of executing the final approved prompt. |

Operator-mediated collaboration uses a separate lifecycle so the approval gate
is server-enforced rather than a UI convention:

| Tool | Caller | Purpose |
|---|---|---|
| `collab_propose_message` | collaboration-governed agent | Creates a pending draft for operator review. |
| `collab_poll` | collaboration-governed recipient | Returns approved, unacked deliverables addressed to the caller. |
| `collab_ack` | collaboration-governed recipient | Acknowledges a delivered collaboration message. |
| `collab_get_thread` | collaboration participant/operator | Returns the caller-visible thread projection. |

Every worker, arbitrator, and reviewer response must conform to the structured JSON contract in [`DESIGN.md` §6](./DESIGN.md#6-agent-output-contract). Malformed responses cause the iteration to be marked `off_script` and pause the session for operator resolution.

## Project Structure

```
agent-broker/
  .env.example          # Environment config template
  DESIGN.md             # Authoritative architecture contract (v2)
  Dockerfile            # Container image
  docker-compose.yml    # Production deployment
  docker-compose.tailscale.yml # Optional private-network port binding
  config/
    agents.json
    operator_permanently_hidden_threads.json.example
  src/agent_broker/
    config.py           # Configuration loader (single import point)
    database.py         # SQLite layer + signature helpers
    main.py             # FastAPI app + lifespan
    mcp_server.py       # MCP tool definitions (poll_inbox, emit_response, emit_review, executor_emit)
    workflow.py         # State machine: convergence detection + round controllers
    models.py           # Pydantic request/response models
    routes/
      api.py            # REST endpoints (/api/v1/session/*)
      web.py            # Web UI routes (/web/*)
    templates/          # Jinja2 HTML templates
    static/             # CSS
  tests/
  AGENTS.md             # CLI agent execution contract (for agents working on this repo)
  BOOTSTRAP.md          # Agent self-setup guide (secrets, MCP, verification)
  README.md
```
