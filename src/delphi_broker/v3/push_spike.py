"""Phase-1 push spike — empirical test of MCP server→client notification surfacing.

Adds three test tools to FastMCP that exercise different server-push paths.
The point is to observe what Claude Code (and other MCP clients) actually
do when each is invoked — the result determines whether v3 can rely on
broker-initiated push or needs to fall back to per-task polling-loop prompts.

Tools (all unauthenticated for spike convenience — DO NOT ship to v3 prod):
  - delphi_v3_spike_log         — ctx.log inside a tool call
  - delphi_v3_spike_log_delayed — ctx.log after a 3s sleep mid-call
  - delphi_v3_spike_progress    — multiple progress notifications mid-call
  - delphi_v3_spike_elicit      — server asks the client a question

Each tool returns a structured result documenting what was sent, so the
caller can correlate the broker-side trace with what their MCP client did
client-side.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.fastmcp import Context


def register_spike_tools(mcp) -> None:
    """Attach push-spike tools to the FastMCP server.

    Called from mcp_server.py during module load. Idempotent — re-registering
    is fine in dev reloads.
    """

    @mcp.tool(
        name="delphi_v3_spike_log",
        description=(
            "Phase-1 spike. Emits a single `notifications/message` log to the "
            "calling client's MCP session. Returns the timestamps so caller "
            "can correlate with what their CLI surfaced."
        ),
    )
    async def spike_log(message: str, ctx: Context) -> dict[str, Any]:
        sent_at = time.time()
        await ctx.log(level="info", message=f"[delphi-broker spike] {message}")
        return {
            "kind": "log",
            "message": message,
            "sent_at": sent_at,
            "note": (
                "If your CLI surfaced the log message in chat, "
                "ctx.log push works as a wake-up signal."
            ),
        }

    @mcp.tool(
        name="delphi_v3_spike_log_delayed",
        description=(
            "Phase-1 spike. Sleeps 3s mid-call, then emits a log. Tests "
            "whether the SSE channel delivers a mid-flight notification "
            "before the tool call returns."
        ),
    )
    async def spike_log_delayed(message: str, ctx: Context) -> dict[str, Any]:
        started_at = time.time()
        await asyncio.sleep(3.0)
        log_at = time.time()
        await ctx.log(
            level="info",
            message=f"[delphi-broker spike — delayed] {message}",
        )
        return_at = time.time()
        return {
            "kind": "log_delayed",
            "message": message,
            "started_at": started_at,
            "log_at": log_at,
            "return_at": return_at,
        }

    @mcp.tool(
        name="delphi_v3_spike_progress",
        description=(
            "Phase-1 spike. Emits 5 progress notifications over 3 seconds. "
            "Tests whether progress messages surface as ambient updates "
            "in the calling CLI."
        ),
    )
    async def spike_progress(ctx: Context) -> dict[str, Any]:
        started_at = time.time()
        for i in range(5):
            # ServerSession.send_progress_notification expects:
            # progress_token (any), progress, total, message
            await ctx.report_progress(
                progress=i + 1,
                total=5,
                message=f"step {i+1} of 5",
            )
            await asyncio.sleep(0.6)
        return {
            "kind": "progress",
            "steps": 5,
            "started_at": started_at,
            "elapsed_s": time.time() - started_at,
        }

    @mcp.tool(
        name="delphi_v3_spike_elicit",
        description=(
            "Phase-1 spike. Server asks the client a question via "
            "Context.elicit. If the CLI surfaces the question to the user "
            "and waits for a response, elicit is the cleanest wake-up "
            "primitive available."
        ),
    )
    async def spike_elicit(ctx: Context) -> dict[str, Any]:
        try:
            from pydantic import BaseModel

            class Reply(BaseModel):
                acknowledged: bool
                note: str | None = None

            result = await ctx.elicit(
                message=(
                    "[delphi-broker spike] The broker would push a dispatch "
                    "here. Acknowledge to confirm your CLI surfaced this prompt."
                ),
                schema=Reply,
            )
            return {
                "kind": "elicit",
                "result_action": getattr(result, "action", None),
                "result_data": (
                    result.data.model_dump() if getattr(result, "data", None) else None
                ),
            }
        except Exception as exc:  # noqa: BLE001 -- spike, log everything
            return {
                "kind": "elicit",
                "error": f"{type(exc).__name__}: {exc}",
                "note": (
                    "If this raised because the client doesn't support elicit, "
                    "we know elicit is not a viable wake-up primitive for this CLI."
                ),
            }
