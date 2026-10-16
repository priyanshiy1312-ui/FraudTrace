"""FraudTrace MCP server (official `mcp` SDK, FastMCP).

    MCP_TRANSPORT=stdio             python -m mcp_server.server    # used by tests / local clients
    MCP_TRANSPORT=streamable-http   python -m mcp_server.server    # used by docker compose (port 8001)

Each tool is a thin wrapper over mcp_server/tools.py. Read tools are annotated read-only,
action tools are annotated as writing, so any MCP client can tell them apart.
"""
from __future__ import annotations

import functools
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_server import tools

mcp = FastMCP("fraudtrace", instructions="Graph tools over the FraudTrace FalkorDB graphs.",
              host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("MCP_PORT", "8001")))


def _register(fn, *, read_only: bool) -> None:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (tools.ToolError, ValueError) as exc:     # bad input -> readable tool error for the model
            raise ValueError(str(exc)) from None
    mcp.tool(annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=not read_only and fn is not tools.save_finding,
                                         idempotentHint=read_only, openWorldHint=False))(wrapper)


for _fn in tools.READ_TOOLS + [tools.score_account, tools.recall_past_findings]:
    _register(_fn, read_only=True)
for _fn in [tools.save_finding, tools.record_feedback] + tools.ACTION_TOOLS:
    _register(_fn, read_only=False)

if __name__ == "__main__":
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))   # type: ignore[arg-type]
