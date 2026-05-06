# Agent Broker — Agent Bootstrap Guide

**Purpose:** Step-by-step instructions for CLI agents to self-configure as
Agent Broker clients. Read this file and execute the steps for your assigned
agent identity.

The architecture this guide configures you for is described in
[`DESIGN.md`](./DESIGN.md). If anything below contradicts `DESIGN.md`, escalate
to the operator before proceeding.

---

## Roles

Every registered agent has exactly one role from `worker | arbitrator | executor`
(enforced by the `agents.role` schema check). The default deployment looks like
this:

| Role | Count | Default agent_ids | Notes |
|------|-------|-------------------|-------|
| `worker` | 4 | `prod-claude`, `prod-codex`, `dev-claude`, `dev-codex` | One Claude + one Codex per host |
| `arbitrator` | 1 | `flow-claude` | Cross-host synthesis (round 2) |
| `executor` | 1 | `dev-codex-executor` (recommended) | Runs the final approved prompt |

**Role constraint:** because each `agent_id` carries exactly one role, the
executor cannot be the same identity as a worker. If the operator wants the
dev-host Codex to be the executor, register a *separate* identity (e.g.
`dev-codex-executor`) that reuses the same Codex CLI under a distinct env file.
`DELPHI_EXECUTOR_AGENT_ID` in `.env` selects which identity is the executor.

---

## Prerequisites

You need these pieces of information from your operator before starting:

| Parameter | Example | How you get it |
|-----------|---------|----------------|
| `AGENT_ID` | `dev-codex` | Assigned by operator |
| `ROLE` | `worker` | Assigned by operator (`worker`, `arbitrator`, or `executor`) |
| `BROKER_IP` | `100.81.33.20` or `localhost` | `localhost` if you're on the broker host, Tailscale IP otherwise |

---

## Network Reference

| Host | Tailscale IP | Agents | Notes |
|------|-------------|--------|-------|
| **BSPiLHX (Pi 5)** | **`100.81.33.20`** | `pi-claude` | **Broker host** — use `localhost` |
| Nucbox (desktop-vc94kou) | `100.80.8.34` | `dev-codex`, `dev-claude`, `dev-codex-executor` | Remote — use `100.81.33.20` |
| Alienware (bsalien) | `100.67.243.54` | `prod-codex`, `prod-claude` | Remote — use `100.81.33.20` |
| BSFlow | `100.89.34.67` | `bsflow-claude` | Remote — use `100.81.33.20` |

> ⚠️ **Historical note:** earlier drafts of this doc had Nucbox as the broker host
> (per the v2 design's `100.80.8.34` reference). The deployment moved to Pi for
> 24/7 availability — the broker is now on `100.81.33.20`. If your local
> `~/.claude.json` (or Codex equivalent) was created via `claude mcp add`
> against the older table, it has the stale Nucbox URL baked in. Remove and
> re-add the MCP server with the Pi URL.

---

## Phase 1 — Generate Secret & Configure

Complete all steps. Do NOT proceed to Phase 2 until instructed.

### Step 1: Generate your HMAC secret

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

If `python` is not found, try `python3`. Save the output — this is your
64-character hex signing secret. Do not log it, commit it, or expose it.

### Step 2: Create your agent env file

Each agent gets its own named file under a shared `delphi-agent/` directory in
your host's projects root (e.g. `C:\Projects\delphi-agent\` on Windows,
`~/projects/delphi-agent/` on Linux). This avoids collisions when multiple
agents share a host.

```bash
mkdir -p <PROJECTS_ROOT>/delphi-agent
cat > <PROJECTS_ROOT>/delphi-agent/<AGENT_ID>.env << 'EOF'
DELPHI_AGENT_ID=<AGENT_ID>
DELPHI_AGENT_SECRET=<YOUR_GENERATED_SECRET>
DELPHI_BROKER_URL=http://<BROKER_IP>:8420
EOF
chmod 600 <PROJECTS_ROOT>/delphi-agent/<AGENT_ID>.env
```

Replace `<PROJECTS_ROOT>`, `<AGENT_ID>`, `<YOUR_GENERATED_SECRET>`, and
`<BROKER_IP>` with your actual values.

Example for `dev-codex` on Nucbox (Linux):
```bash
mkdir -p ~/projects/delphi-agent
cat > ~/projects/delphi-agent/dev-codex.env << 'EOF'
DELPHI_AGENT_ID=dev-codex
DELPHI_AGENT_SECRET=a1b2c3...your64charhex...
DELPHI_BROKER_URL=http://localhost:8420
EOF
chmod 600 ~/projects/delphi-agent/dev-codex.env
```

Example for `prod-codex` on Alienware (Windows/Git Bash):
```bash
mkdir -p /c/Projects/delphi-agent
cat > /c/Projects/delphi-agent/prod-codex.env << 'EOF'
DELPHI_AGENT_ID=prod-codex
DELPHI_AGENT_SECRET=a1b2c3...your64charhex...
DELPHI_BROKER_URL=http://100.80.8.34:8420
EOF
```

### Step 3: Add MCP server config

Merge the following into `~/.claude/settings.json` under `mcpServers`.
Do NOT overwrite existing entries — add alongside them.

```json
{
  "mcpServers": {
    "agent-broker": {
      "type": "url",
      "url": "http://<BROKER_IP>:8420/mcp"
    }
  }
}
```

If the file doesn't exist, create it with just this content.

### Step 4: Report Phase 1 results

Reply to your operator with exactly:

```
agent_id: <AGENT_ID>
role: <ROLE>
secret: <YOUR_GENERATED_SECRET>
env_file: ✅ or ❌
mcp_config: ✅ or ❌
```

**STOP HERE.** The operator must collect all secrets, populate
`config/agents.json` (and optionally `config/agents-secrets.json`) on the broker
host, and start the broker before you can verify.

---

## Phase 2 — Verify (wait for operator go signal)

The broker must be running with your secret registered before these steps work.
v2 has no public connectivity-test endpoint, so verification goes through the
MCP tools.

### Step 1: Restart your CLI to pick up the MCP config

If your CLI was already running when you edited `~/.claude/settings.json`,
restart it now so the `agent-broker` server is loaded.

### Step 2: Call `delphi_poll_inbox`

This is the v2 equivalent of the v1 connectivity test. It exercises the same
auth path (HMAC signature, replay window, agent registry lookup) without
mutating any state.

The signature canonical for `poll_inbox` is:

```
poll_inbox|<AGENT_ID>|<TIMESTAMP>
```

Example one-shot from the shell, useful when you want to verify before
restarting an MCP-aware CLI:

```bash
source <PROJECTS_ROOT>/delphi-agent/<AGENT_ID>.env

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
CANONICAL="poll_inbox|${DELPHI_AGENT_ID}|${TIMESTAMP}"
SIGNATURE=$(echo -n "$CANONICAL" | openssl dgst -sha256 -hmac "${DELPHI_AGENT_SECRET}" | awk '{print $2}')

echo "agent_id=${DELPHI_AGENT_ID}"
echo "client_ts=${TIMESTAMP}"
echo "signature=${SIGNATURE}"
```

When you have an MCP-aware CLI loaded, call:

```
delphi_poll_inbox(agent_id="<AGENT_ID>", client_ts="<TIMESTAMP>", signature="<SIGNATURE>")
```

**Expected on success:** `{"iterations": [], "reviews_pending": []}` (both
empty lists are fine — they only fill in when a real session is running and
your agent is the destination of a pending iteration).

**If you get `{"error": "auth_failed", "reason": "invalid signature"}`:** your
secret doesn't match what the operator registered. Report verbatim — do not
retry with a different secret.

**If you get `{"error": "auth_failed", "reason": "unknown agent_id ..."}`:**
the operator hasn't added you to `config/agents.json` yet, or the broker hasn't
been restarted to pick it up.

The operator can confirm your call hit the broker by checking the broker logs
(every authenticated MCP call is recorded with the agent_id).

Once a real session is started, your agent will see iterations in
`delphi_poll_inbox` automatically — no further action on your side beyond
polling and responding.

### Step 3: Report Phase 2 results

```
agent_id: <AGENT_ID>
role: <ROLE>
mcp_loaded: ✅ or ❌
poll_inbox_ok: ✅ or ❌
errors: <any errors verbatim, or "none">
```

---

## Agent Output Format

Every response your agent emits via `delphi_emit_response` must conform to the
structured contract from `DESIGN.md` §6. Malformed responses cause the
broker to mark the iteration `off_script` and pause the round for the operator.

### Worker / arbitrator (round 1, round 2)

```json
{
  "output": "<the refined prompt or synthesis>",
  "self_assessment": "CONVERGED | MORE_WORK_NEEDED",
  "rationale": "<why CONVERGED or what still needs work>"
}
```

### Reviewer (round 3 only — call `delphi_emit_review`)

```json
{
  "decision": "APPROVE | REJECT",
  "comments": "<required if REJECT, optional if APPROVE>",
  "rationale": "<reasoning>"
}
```

### Executor (call `delphi_executor_emit`)

The executor reports execution success/failure with `success: bool`, the
`output` of the run, and an optional `error` string when `success=false`. See
the MCP tool docstring for the exact field set.

The broker does not auto-correct malformed agent output — it pauses and
escalates to the operator.

---

## Broker Host Only (Nucbox)

If you are an agent on the broker host, one of you must also start the broker
before Phase 2. Coordinate with the operator on which agent does this.

```bash
cd ~/agent-broker  # or wherever the repo is cloned

# Ensure .env exists with DELPHI_OPERATOR_TOKEN set
test -f .env || { echo "ERROR: .env not found"; exit 1; }
grep -q '^DELPHI_OPERATOR_TOKEN=' .env || { echo "ERROR: DELPHI_OPERATOR_TOKEN not set in .env"; exit 1; }

# Ensure config/agents.json (and agents-secrets.json if used) is populated
test -f config/agents.json || { echo "ERROR: config/agents.json not found"; exit 1; }

# Create data directory for SQLite persistence
mkdir -p data

# Build and start
docker compose -p agent-broker up -d --build

# Verify the container is up (no public unauth endpoint to probe in v2 —
# operator should hit /web/ with the operator token)
sleep 3
docker compose -p agent-broker ps
```

---

## HMAC Signature Protocol Reference

All authority-bearing MCP calls require an HMAC-SHA256 signature. The canonical
format is pipe-delimited fields with an action prefix.

| Action | Canonical format |
|--------|-----------------|
| `poll_inbox` | `poll_inbox\|agent_id\|client_ts` |
| `emit_response` | see `database.build_emit_response_signature_fields` (`emit_response\|agent_id\|iteration_id\|client_ts\|output\|self_assessment\|rationale`) |
| `emit_review` | see `database.build_emit_review_signature_fields` (`emit_review\|agent_id\|round_id\|client_ts\|decision\|comments\|rationale`) |
| `executor_emit` | see `database.build_executor_emit_signature_fields` (`executor_emit\|agent_id\|iteration_id\|client_ts\|success\|output\|error`) |

Signature = `HMAC-SHA256(agent_secret, "|".join(canonical_fields))`

Timestamps must be ISO 8601 and within 5 minutes of broker time (replay
protection). The deleted v1 actions (`submit`, `approve`, `reject`, `ack`,
`broadcast`) are gone.

---

## Constraints

- Do NOT modify the broker repo or any broker-side config
- Do NOT commit secrets to any repository
- Do NOT push changes — this is local-only configuration
- Secrets are per-agent, not per-host — two agents on the same host get separate env files
- The env file at `<PROJECTS_ROOT>/delphi-agent/<AGENT_ID>.env` must be mode 600
- The `delphi-agent/` directory is NOT a git repo — it's local config only, never committed anywhere
- Each agent has exactly one role; if you need to reuse a CLI for both worker and executor duties, register two distinct `agent_id`s with separate env files
