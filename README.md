# Delphi Broker

Iterative-pipeline coordinator for a hierarchical multi-agent prompt-refinement workflow. The broker mechanizes the operator's manual copy-paste between CLI agents (Codex, Claude Code) running across Tailscale-connected hosts, while preserving every human nudge point.

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
  - `/mcp` — MCP server exposing four agent tools: `delphi_poll_inbox`, `delphi_emit_response`, `delphi_emit_review`, `delphi_executor_emit`
  - `/web/` — Phone-friendly operator UI (session list, pending nudge, transcript, escalation resolution)
- **SQLite** (WAL mode) is the single source of truth. Schema: `sessions`, `rounds`, `iterations`, `reviews`, `agents`. The v1 `messages`/`message_receipts` tables are gone.
- **Authentication.** Operator: shared `DELPHI_OPERATOR_TOKEN` (cookie-based session for web, header for REST). Agents: per-agent HMAC-SHA256 over canonical fields, with a 5-minute replay window on `client_ts`.
- **Concurrency.** Round 1 runs in parallel across hosts but serially within a host; Round 3 runs in parallel across reviewers. See [`DESIGN.md` §10](./DESIGN.md#10-concurrency-model).

## Configuration

Infrastructure config lives in `.env` (copy from `.env.example`):

```
# Required
DELPHI_OPERATOR_TOKEN=change-me-to-a-random-string

# Network
DELPHI_HOST=0.0.0.0
DELPHI_PORT=8420

# Storage
DELPHI_DB_PATH=delphi.db

# Agent identity
DELPHI_AGENTS_PATH=config/agents.json
DELPHI_AGENT_SECRETS_PATH=config/agents-secrets.json
DELPHI_ARBITRATOR_AGENT_ID=flow-claude
DELPHI_EXECUTOR_AGENT_ID=dev-codex-executor

# Web UI
DELPHI_WEB_SECURE=false   # set true when fronted by HTTPS
```

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DELPHI_OPERATOR_TOKEN` | yes | — | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DELPHI_HOST` | no | `0.0.0.0` | |
| `DELPHI_PORT` | no | `8420` | |
| `DELPHI_DB_PATH` | no | `delphi.db` | Resolved relative to project root unless absolute |
| `DELPHI_AGENTS_PATH` | no | `config/agents.json` | Public agent manifest (committed) |
| `DELPHI_AGENT_SECRETS_PATH` | no | `config/agents-secrets.json` | Optional sidecar; preferred for production secrets |
| `DELPHI_ARBITRATOR_AGENT_ID` | no | `flow-claude` | Must exist with `role='arbitrator'` |
| `DELPHI_EXECUTOR_AGENT_ID` | no | `dev-codex` | Must exist with `role='executor'` (cannot also be a worker — see `DESIGN.md` §2) |
| `DELPHI_WEB_SECURE` | no | `false` | `true`/`1`/`yes` flags the operator session cookie `Secure` |

Agent registry lives in `config/agents.json` (copy from example):

```bash
cp config/agents.json.example config/agents.json
```

Each agent entry declares `agent_id`, `host`, and exactly one `role` from `worker | arbitrator | executor`. Per-agent HMAC secrets can be inlined (development) or kept in `config/agents-secrets.json` (production).

## Running

### Docker (production)

```bash
cp .env.example .env  # fill in DELPHI_OPERATOR_TOKEN
docker compose -p delphi-broker up -d --build
```

> The `-p delphi-broker` flag isolates this stack from other compose projects on the same host.

Data persists in `./data/` (SQLite DB). Agent registry is mounted read-only from `./config/`.

### Local (development)

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in DELPHI_OPERATOR_TOKEN
python -m uvicorn delphi_broker.main:app --host 0.0.0.0 --port 8420 --app-dir src
```

## MCP Client Configuration

Add to `~/.claude/settings.json` on each agent host:

```json
{
  "mcpServers": {
    "delphi-broker": {
      "type": "url",
      "url": "http://<broker-host>:8420/mcp"
    }
  }
}
```

See [`BOOTSTRAP.md`](./BOOTSTRAP.md) for the full agent self-configuration flow.

## Agent Contract

Agents interact with the broker through four MCP tools. All four require an `agent_id`, a fresh `client_ts` (ISO 8601, within 5 minutes of broker time), and an HMAC-SHA256 `signature` over the per-action canonical field set.

| Tool | Caller | Purpose |
|---|---|---|
| `delphi_poll_inbox` | any agent | Returns pending iterations (where the agent is the destination) and pending review requests. Polled regularly by every connected agent. |
| `delphi_emit_response` | worker / arbitrator | Submits a structured response (`output` + `self_assessment` + `rationale`) for an open iteration. |
| `delphi_emit_review` | round-3 reviewer (worker) | Submits `APPROVE` or `REJECT` with optional comments and rationale. |
| `delphi_executor_emit` | executor | Reports the success/failure of executing the final approved prompt. |

Every worker, arbitrator, and reviewer response must conform to the structured JSON contract in [`DESIGN.md` §6](./DESIGN.md#6-agent-output-contract). Malformed responses cause the iteration to be marked `off_script` and pause the session for operator resolution.

## Project Structure

```
delphi-broker/
  .env.example          # Environment config template
  DESIGN.md             # Authoritative architecture contract (v2)
  Dockerfile            # Container image
  docker-compose.yml    # Production deployment
  config/
    agents.json.example
    agents-secrets.json.example
  src/delphi_broker/
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
