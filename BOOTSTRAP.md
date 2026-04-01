# Delphi Broker — Agent Bootstrap Guide

**Purpose:** Step-by-step instructions for CLI agents to self-configure as
Delphi Broker clients. Read this file and execute the steps for your assigned
agent identity.

---

## Prerequisites

You need two pieces of information from your operator before starting:

| Parameter | Example | How you get it |
|-----------|---------|----------------|
| `AGENT_ID` | `dev-codex` | Assigned by operator |
| `BROKER_IP` | `100.80.8.34` or `localhost` | `localhost` if you're on the broker host, Tailscale IP otherwise |

---

## Network Reference

| Host | Tailscale IP | Agents | Notes |
|------|-------------|--------|-------|
| Nucbox (desktop-vc94kou) | `100.80.8.34` | `dev-codex`, `dev-claude` | **Broker host** — use `localhost` |
| Alienware (bsalien) | `100.67.243.54` | `prod-codex`, `prod-claude` | Remote — use `100.80.8.34` |
| BSFlow | `100.89.34.67` | `flow-claude` | Remote — use `100.80.8.34` |

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
    "delphi-broker": {
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
secret: <YOUR_GENERATED_SECRET>
env_file: ✅ or ❌
mcp_config: ✅ or ❌
```

**STOP HERE.** The operator must collect all secrets, populate `config/agents.json`
on the broker host, and start the broker before you can verify.

---

## Phase 2 — Verify (wait for operator go signal)

The broker must be running with your secret registered before these steps work.

### Step 1: Verify connectivity

```bash
curl -sf http://<BROKER_IP>:8420/api/v1/agents | python -c "
import sys, json
agents = json.load(sys.stdin)
print(f'{len(agents)} agents registered')
for a in agents:
    print(f'  {a[\"agent_id\"]} ({a[\"roles\"]})')
"
```

You should see your `AGENT_ID` in the list.

### Step 2: Verify HMAC signing

This submits a test message to confirm your secret matches the broker's registry.

```bash
source <PROJECTS_ROOT>/delphi-agent/<AGENT_ID>.env

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
CANONICAL="submit|${DELPHI_AGENT_ID}|deployment-test|${TIMESTAMP}|ping|connectivity test from ${DELPHI_AGENT_ID}|*|normal||{}"
SIGNATURE=$(echo -n "$CANONICAL" | openssl dgst -sha256 -hmac "${DELPHI_AGENT_SECRET}" | awk '{print $2}')

curl -s -X POST "${DELPHI_BROKER_URL}/api/v1/messages" \
  -H "Content-Type: application/json" \
  -d "{
    \"sender\": \"${DELPHI_AGENT_ID}\",
    \"channel\": \"deployment-test\",
    \"subject\": \"ping\",
    \"body\": \"connectivity test from ${DELPHI_AGENT_ID}\",
    \"recipients\": \"*\",
    \"priority\": \"normal\",
    \"timestamp\": \"${TIMESTAMP}\",
    \"signature\": \"${SIGNATURE}\"
  }"
```

**Expected:** `{"message_id": "<uuid>", "status": "PENDING", ...}`

**If you get `"Invalid signature — rejected"`:** Your secret doesn't match what
the operator put in `config/agents.json`. Report the error — do not retry with
a different secret.

### Step 3: Verify MCP tools (optional)

If your CLI supports MCP, restart it to pick up the new server config, then call:

```
delphi_pending
```

Expected: `{"count": <N>, "messages": [...]}`

### Step 4: Report Phase 2 results

```
agent_id: <AGENT_ID>
connectivity: ✅ or ❌
hmac_test: ✅ or ❌
mcp_tools: ✅ or ❌ or ⏭️ (skipped)
message_id: <uuid from step 2, if successful>
errors: <any errors verbatim, or "none">
```

---

## Broker Host Only (Nucbox)

If you are an agent on the broker host, one of you must also start the broker
before Phase 2. Coordinate with the operator on which agent does this.

```bash
cd ~/delphi-broker  # or wherever the repo is cloned

# Ensure .env exists with a real password
# (operator should have already created this)
test -f .env || { echo "ERROR: .env not found"; exit 1; }

# Ensure config/agents.json has all secrets populated
test -f config/agents.json || { echo "ERROR: config/agents.json not found"; exit 1; }

# Create data directory for SQLite persistence
mkdir -p data

# Build and start
docker compose up -d --build

# Verify broker is responding
sleep 3
curl -sf http://localhost:8420/api/v1/agents && echo " — broker is up" || echo " — broker failed to start"
```

---

## HMAC Signature Protocol Reference

All authority-bearing mutations require HMAC-SHA256 signatures. The canonical
format is pipe-delimited fields with an action prefix:

| Action | Canonical format |
|--------|-----------------|
| `submit` | `submit\|sender\|channel\|timestamp\|subject\|body\|recipients\|priority\|parent_id\|metadata_json` |
| `approve` | `approve\|agent_id\|message_id\|timestamp\|note` |
| `reject` | `reject\|agent_id\|message_id\|timestamp\|reason` |
| `ack` | `ack\|agent_id\|message_id\|timestamp` |
| `broadcast` | `broadcast\|sender\|channel\|timestamp\|subject\|body\|priority\|auto_approve` |

Signature = `HMAC-SHA256(agent_secret, canonical_string)`

Timestamps must be ISO 8601 and within 5 minutes of server time (replay protection).

---

## Constraints

- Do NOT modify the broker repo or any broker-side config
- Do NOT commit secrets to any repository
- Do NOT push changes — this is local-only configuration
- Secrets are per-agent, not per-host — two agents on the same host get separate env files
- The env file at `<PROJECTS_ROOT>/delphi-agent/<AGENT_ID>.env` must be mode 600
- The `delphi-agent/` directory is NOT a git repo — it's local config only, never committed anywhere
