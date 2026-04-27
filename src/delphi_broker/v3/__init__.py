"""Delphi Broker v3 — central-orchestration model.

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
