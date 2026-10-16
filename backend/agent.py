"""The FraudTrace agent: Claude + MCP tools, one investigation at a time.

Loop: (prefetch memory) -> model picks a tool -> we call it over MCP -> result goes back to the model ->
repeat until the model calls `submit_verdict`. Every step is reported through `emit(event)` so the UI can
stream it. The model never sees or chooses `analyst_id` / `investigation_id`: we inject them (tenant isolation).
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from mcp_server import tools as graph_tools

MAX_STEPS = 14
MAX_RESULT_CHARS = 6000                       # what a single tool result may cost in model context
HIDDEN_ARGS = {"analyst_id", "investigation_id"}
ACTION_NAMES = {"flag_accounts", "hold_transaction", "create_case"}
READ_ONLY_EVIDENCE_TOOLS = {"get_account_profile", "get_transaction_links", "get_neighbors", "find_shared_entities",
                            "shortest_path", "detect_communities", "rank_suspicious_nodes", "detect_cycles",
                            "find_mule_candidates", "run_readonly_cypher", "score_account"}
ENTITY_RE = re.compile(r"\b(?:A|T|D|IP|M|P)-\d{1,8}\b")

SYSTEM_PROMPT = """You are FraudTrace, a fraud-investigation agent for a bank analyst. You reason over a FalkorDB \
graph (accounts, devices, IPs, merchants, transactions) through tools. All data is synthetic.

How to work:
1. Memory first: findings already remembered for the entities in the request are provided below. Use them and build on \
them instead of starting over. Call recall_past_findings for any NEW entity you start to investigate. If memory holds a \
'false_positive' verdict about an entity, do not flag it again unless you find NEW evidence, and say so.
2. Choose tools yourself. Start from the request, expand with multi-hop reasoning (shared devices/IPs, money flow), then \
use graph algorithms (detect_communities, detect_cycles, find_mule_candidates, shortest_path, rank_suspicious_nodes). \
Use run_readonly_cypher for anything the tools don't cover. Make at least 3 tool calls for a non-trivial question.
3. Before EVERY tool call write one short sentence saying why (the analyst sees it live).
4. Score the main suspects with score_account; the final risk_score must come from it.
5. Take actions (flag_accounts, hold_transaction, create_case) when the evidence is strong, citing the evidence as short \
strings (tool names + facts). The tools enforce an evidence threshold: if they refuse, accept it, do not retry.
6. Call save_finding for each conclusion (list every involved entity id) BEFORE submit_verdict, so it is remembered.
7. Finish by calling submit_verdict. Be honest: list what you did not verify in `limitations`. Never invent ids."""

SUBMIT_TOOL = {
    "name": "submit_verdict",
    "description": "Finish the investigation. Call exactly once, after saving findings and taking actions.",
    "input_schema": {"type": "object", "properties": {
        "verdict": {"type": "string", "description": "One of: fraud_ring | mule_network | synthetic_identity | suspicious | likely_benign | inconclusive"},
        "summary": {"type": "string", "description": "3-6 sentence plain-English explanation for the analyst."},
        "risk_score": {"type": "integer", "description": "0-100, taken from score_account."},
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "evidence_paths": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                           "description": "Each path is a list of node ids, e.g. ['A-1','D-9','A-2']."},
        "confidence": {"type": "number", "description": "0-1"},
        "limitations": {"type": "array", "items": {"type": "string"}}},
        "required": ["verdict", "summary", "risk_score", "key_findings", "evidence_paths", "confidence", "limitations"]},
}

Emit = Callable[[dict], Awaitable[None]]


@asynccontextmanager
async def mcp_session():
    """Connect to the MCP server: over HTTP when MCP_URL is set (docker), otherwise spawn it over stdio."""
    url = os.getenv("MCP_URL", "")
    if url and url != "stdio":
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s
    else:
        params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                       env={**os.environ, "MCP_TRANSPORT": "stdio"})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s


def to_model_tools(mcp_tools) -> list[dict]:
    """MCP tool list -> Anthropic tool list, hiding the arguments the backend injects."""
    out = []
    for t in mcp_tools:
        schema = json.loads(json.dumps(t.inputSchema))
        props = {k: v for k, v in schema.get("properties", {}).items() if k not in HIDDEN_ARGS}
        schema["properties"] = props
        schema["required"] = [r for r in schema.get("required", []) if r not in HIDDEN_ARGS]
        out.append({"name": t.name, "description": (t.description or "")[:900], "input_schema": schema})
    return out


def _blocks_to_param(content) -> list[dict]:
    blocks = []
    for b in content:
        if b.type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


def _parse_result(res) -> tuple[bool, dict | str]:
    text = "".join(getattr(c, "text", "") for c in res.content)
    if res.isError:
        return False, text
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        return True, text


def _clip(obj: Any, limit: int | None = None) -> str:
    limit = limit or MAX_RESULT_CHARS
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + f'... [truncated {len(s) - limit} chars]'


async def run_investigation(analyst_id: str, question: str, emit: Emit, client, model: str,
                            session_factory=mcp_session) -> dict:
    inv_id = graph_tools.start_investigation(analyst_id, question)
    await emit({"type": "start", "investigation_id": inv_id, "analyst": analyst_id, "question": question})

    cypher_used: list[str] = []
    actions: list[dict] = []
    memory_used: list[dict] = []
    scores: list[int] = []
    evidence_seen = False
    n_calls = 0

    async with session_factory() as session:
        raw_tools = (await session.list_tools()).tools
        model_tools = to_model_tools(raw_tools) + [SUBMIT_TOOL]
        # which injected args each real MCP tool accepts (we only inject what the tool declares)
        INJECT_ANALYST = {t.name for t in raw_tools if "analyst_id" in t.inputSchema.get("properties", {})}
        INJECT_INVESTIGATION = {t.name for t in raw_tools if "investigation_id" in t.inputSchema.get("properties", {})}

        async def call(name: str, args: dict, tool_use_id: str):
            """Call one MCP tool with injected identity, report it, and return (ok, payload)."""
            nonlocal evidence_seen, n_calls
            clean = {k: v for k, v in args.items() if k not in HIDDEN_ARGS}
            await emit({"type": "tool_call", "id": tool_use_id, "name": name, "args": clean,
                        "action": name in ACTION_NAMES})
            full = dict(clean)
            if name in INJECT_ANALYST:
                full["analyst_id"] = analyst_id
            if name in INJECT_INVESTIGATION:
                full["investigation_id"] = inv_id
            n_calls += 1
            if name in ACTION_NAMES and not evidence_seen:
                ok, payload = False, "refused: no graph evidence gathered yet; use read tools first"
            else:
                ok, payload = _parse_result(await session.call_tool(name, full))
            summary, sub, cypher, extra = "", {"nodes": [], "edges": []}, [], {}
            if ok and isinstance(payload, dict):
                data = payload.get("data", payload)
                cypher = payload.get("cypher", [])
                sub = payload.get("subgraph", sub)
                summary = _clip(data, 700)
                cypher_used.extend(q for q in cypher if q not in cypher_used)
                if name in READ_ONLY_EVIDENCE_TOOLS:
                    evidence_seen = True
                if name == "score_account":
                    scores.append(int(data["final_score"]))
                    extra = {"score": {"account": data["account"], "final": data["final_score"],
                                       "base": data["base_score"], "memory_adjustment": data["memory_adjustment"]}}
                if name == "recall_past_findings" and data.get("findings"):
                    memory_used.append({"entity": data["entity"], "findings": data["findings"]})
                    await emit({"type": "memory", "entity": data["entity"], "findings": data["findings"]})
                if name in ACTION_NAMES:
                    actions.append({"tool": name, "args": clean, "result": data})
            else:
                summary = _clip(payload, 500)
            await emit({"type": "tool_result", "id": tool_use_id, "name": name, "ok": ok, "summary": summary,
                        "cypher": cypher, "subgraph": sub, "action": name in ACTION_NAMES, **extra})
            return ok, payload

        # ---- memory prefetch: look up every entity mentioned in the question before the model starts ----
        entities = list(dict.fromkeys(ENTITY_RE.findall(question)))[:5]
        remembered: list[str] = []
        for i, ent in enumerate(entities):
            ok, payload = await call("recall_past_findings", {"entity_id": ent}, f"prefetch-{i}")
            if ok and isinstance(payload, dict) and payload["data"]["findings"]:
                remembered.append(_clip(payload["data"], 2500))
        memory_block = ("Memory for entities in the request:\n" + "\n".join(remembered)) if remembered else \
            "Memory for entities in the request: nothing remembered yet."

        messages: list[dict] = [{"role": "user", "content": f"{question}\n\n[{memory_block}]"}]
        verdict_in: dict | None = None
        for step in range(MAX_STEPS + 1):
            force = step == MAX_STEPS
            kwargs = {"tool_choice": {"type": "tool", "name": "submit_verdict"}} if force else {}
            resp = await client.messages.create(model=model, max_tokens=4096, system=SYSTEM_PROMPT,
                                                tools=model_tools, messages=messages, **kwargs)
            messages.append({"role": "assistant", "content": _blocks_to_param(resp.content)})
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    await emit({"type": "thought", "text": b.text.strip()})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:                                              # model stopped talking without a verdict
                messages.append({"role": "user", "content": "Call submit_verdict now."})
                continue
            results = []
            for u in uses:
                if u.name == "submit_verdict":
                    verdict_in = u.input
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": "recorded"})
                    continue
                ok, payload = await call(u.name, dict(u.input), u.id)
                body = payload.get("data", payload) if isinstance(payload, dict) else payload
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": _clip(body), "is_error": not ok})
            messages.append({"role": "user", "content": results})
            if verdict_in is not None:
                break

    v = dict(verdict_in or {"verdict": "inconclusive", "summary": "The agent did not finish.", "key_findings": [],
                            "evidence_paths": [], "confidence": 0.0, "limitations": ["no verdict submitted"]})
    v["risk_score_source"] = "score_account" if scores else "model"
    if scores:
        v["risk_score"] = max(scores)                 # deterministic: comes from the graph + memory, not the model
    v.update({"investigation_id": inv_id, "queries_used": cypher_used, "actions_taken": actions,
              "memory_used": memory_used, "tool_calls": n_calls})
    await emit({"type": "verdict", "verdict": v})
    return v
