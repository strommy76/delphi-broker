"""
--------------------------------------------------------------------------------
FILE:        __init__.py
PATH:        ~/projects/agent-broker/src/agent_broker/v3/__init__.py
DESCRIPTION: Package marker and overview for the Delphi v3 orchestration lane.

CHANGELOG:
2026-05-06 08:30      Codex      [Refactor] Rename package to agent_broker and harden fail-loud Phase 1 broker boundaries.
--------------------------------------------------------------------------------

Agent Broker v3 — central-orchestration model.

v3 collapses v2's hierarchical pipeline (R1 same-host pair → R2 arbitration →
R3 review) into a simpler orchestrator-worker pattern:

    operator -> task -> orchestrator -> dispatches to workers
    workers -> outputs -> orchestrator -> aggregation -> operator

The orchestrator is one of the connected agents (selected per task via the
web UI dropdown). Independence rule: once an agent is chosen as orchestrator
for a task, they are precluded from worker / implementation roles in that
task. eligible_workers(task) = agents - {orchestrator_id}.

This package is built alongside v2; v2 stays for any historical session.
"""
