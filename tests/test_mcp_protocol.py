"""Talk to the real MCP server over stdio, exactly like the agent does."""
import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.usefixtures("live_db")
EXPECTED = {"get_account_profile", "get_transaction_links", "get_neighbors", "find_shared_entities", "shortest_path",
            "detect_communities", "rank_suspicious_nodes", "detect_cycles", "find_mule_candidates",
            "run_readonly_cypher", "score_account", "recall_past_findings", "save_finding", "record_feedback",
            "flag_accounts", "hold_transaction", "create_case"}


def _run(coro):
    return asyncio.run(coro)


def _params():
    return StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                 env={**os.environ, "MCP_TRANSPORT": "stdio"})


def test_tools_are_discoverable_with_schemas_and_annotations():
    async def go():
        async with stdio_client(_params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return (await s.list_tools()).tools
    tools = {x.name: x for x in _run(go())}
    assert EXPECTED <= set(tools)
    assert tools["get_account_profile"].annotations.readOnlyHint is True
    assert tools["flag_accounts"].annotations.readOnlyHint is False
    assert "account_ids" in tools["flag_accounts"].inputSchema["properties"]


def test_call_tool_and_error_path(gt):
    async def go():
        async with stdio_client(_params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                ok = await s.call_tool("get_transaction_links", {"tx_id": "T-9001"})
                bad = await s.call_tool("run_readonly_cypher", {"query": "MATCH (n) DELETE n"})
                return ok, bad
    ok, bad = _run(go())
    assert not ok.isError and "T-9001" in ok.content[0].text
    assert bad.isError and "rejected" in bad.content[0].text
