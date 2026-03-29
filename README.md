# Delphi Broker

Approval-gated MCP message broker for routing communication between CLI agents across Tailscale-connected hosts.

## Purpose

Replaces manual copy-paste routing in a Delphi-method workflow where multiple independent agents (Codex, Claude Code) develop solutions across separate hosts and a human orchestrator synthesizes results via a phone-friendly web UI.

## Architecture

- **FastAPI** application with three surfaces:
  - `/api/v1/` — REST API
  - `/mcp` — MCP (Model Context Protocol) server for Claude Code integration
  - `/web/` — Phone-friendly approval interface
- **SQLite** (WAL mode) for message and agent state
- **Message lifecycle:** `PENDING -> APPROVED/REJECTED -> ACKED`
- **Role-based access:** orchestrator role required for approve/reject/broadcast

## Configuration

Infrastructure config lives in `.env` (copy from `.env.example`):

```
DELPHI_HOST=0.0.0.0
DELPHI_PORT=8420
DELPHI_DB_PATH=delphi.db
```

Agent registry lives in `config/agents.json`:

```json
{
  "agents": [
    {"agent_id": "dev-codex", "host": "desktop-vc94kou", "roles": "worker"},
    {"agent_id": "bsflow-claude", "host": "bsflow", "roles": "worker,orchestrator"}
  ]
}
```

## Running

### Docker (production)

```bash
cp .env.example .env  # adjust as needed
docker compose up -d --build
```

Data persists in `./data/` (SQLite DB). Agent registry is mounted read-only from `./config/`.

### Local (development)

```bash
pip install -r requirements.txt
cp .env.example .env  # adjust as needed
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

## MCP Tools

| Tool | Description | Access |
|------|-------------|--------|
| `delphi_submit` | Submit a message for approval | Any agent |
| `delphi_inbox` | Check inbox for approved messages | Any agent |
| `delphi_pending` | View messages awaiting approval | Any agent |
| `delphi_ack` | Acknowledge receipt of a message | Any agent |
| `delphi_approve` | Approve a pending message | Orchestrator only |
| `delphi_reject` | Reject a pending message | Orchestrator only |
| `delphi_broadcast` | Broadcast to all agents on a channel | Orchestrator only |

## Project Structure

```
delphi-broker/
  .env.example          # Environment config template
  Dockerfile            # Container image
  docker-compose.yml    # Production deployment
  config/
    agents.json         # Seed agent registry (SSOT)
  src/delphi_broker/
    config.py           # Configuration loader (single import point)
    database.py         # SQLite layer
    main.py             # FastAPI app + lifespan
    mcp_server.py       # MCP tool definitions
    models.py           # Pydantic request/response models
    routes/
      api.py            # REST endpoints
      web.py            # Web UI routes
    templates/          # Jinja2 HTML templates
    static/             # CSS
  tests/
  AGENTS.md             # CLI agent execution contract
  README.md
```
