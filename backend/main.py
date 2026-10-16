"""FastAPI app: streams investigations (Server-Sent Events) and serves memory/feedback/score endpoints."""
from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend import agent as agent_module
from backend.agent import run_investigation
from backend.llm import build_client, load_config
from mcp_server import tools as graph_tools
from shared.schema import DEFAULT_ANALYSTS, ensure_memory_schema

LLM = load_config()
MODEL = LLM.model
if LLM.small_context:                       # free tiers have tight token-per-minute limits
    agent_module.MAX_RESULT_CHARS = int(os.getenv("MAX_RESULT_CHARS", "2500"))
ANALYSTS = [a.strip() for a in os.getenv("ANALYSTS", ",".join(DEFAULT_ANALYSTS)).split(",") if a.strip()]
ACCOUNT_RE = re.compile(r"^A-\d{1,8}$")

app = FastAPI(title="FraudTrace API")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://localhost:8080"],
                   allow_methods=["*"], allow_headers=["*"])
_client = None


def get_client():
    global _client
    if _client is None:
        if not LLM.configured:
            raise HTTPException(503, f"No API key for provider '{LLM.provider}' - set it in .env "
                                     "(GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY) and restart the backend")
        _client = build_client(LLM)
    return _client


def check_analyst(analyst_id: str) -> str:
    """Only analysts from the allow-list can be used, so nobody can mint arbitrary memory graphs."""
    if analyst_id not in ANALYSTS:
        raise HTTPException(404, f"unknown analyst '{analyst_id}'")
    return analyst_id


def root_cause(exc: BaseException) -> BaseException:
    """Errors raised inside the MCP session arrive wrapped in an ExceptionGroup; show the real one."""
    while getattr(exc, "exceptions", None):
        exc = exc.exceptions[0]
    return exc


class InvestigateRequest(BaseModel):
    analyst_id: str
    question: str = Field(min_length=3, max_length=500)


class FeedbackRequest(BaseModel):
    analyst_id: str
    finding_id: str = Field(pattern=r"^F-[0-9a-f]{8}$")
    verdict: str = Field(pattern=r"^(confirmed|false_positive)$")


@app.get("/api/health")
def health():
    try:
        graph_tools.db().list_graphs()
        return {"ok": True, "provider": LLM.provider, "model": MODEL, "analysts": ANALYSTS, "api_key_set": LLM.configured}
    except Exception as exc:
        raise HTTPException(503, f"FalkorDB not reachable: {exc}")


@app.get("/api/analysts")
def analysts():
    return {"analysts": ANALYSTS}


@app.post("/api/investigate")
async def investigate(req: InvestigateRequest):
    check_analyst(req.analyst_id)
    client = get_client()
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def worker() -> None:
        # The whole investigation (including its MCP connection) lives in ONE task; the HTTP stream only reads the queue.
        try:
            await run_investigation(req.analyst_id, req.question, emit, client, MODEL)
        except Exception as exc:  # surface any failure to the UI instead of a silent hang
            cause = root_cause(exc)
            await queue.put({"type": "error", "message": f"{type(cause).__name__}: {cause}"})
        finally:
            await queue.put({"type": "done"})

    task = asyncio.create_task(worker())

    async def stream():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] == "done":
                    break
        finally:
            task.cancel()          # client disconnected -> stop the investigation

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/memory/{analyst_id}")
def memory(analyst_id: str):
    check_analyst(analyst_id)
    ensure_memory_schema(graph_tools.db(), analyst_id)
    return graph_tools.list_memory(analyst_id)


@app.get("/api/score/{analyst_id}/{account_id}")
def score(analyst_id: str, account_id: str):
    check_analyst(analyst_id)
    if not ACCOUNT_RE.match(account_id):
        raise HTTPException(422, "bad account id")
    try:
        return graph_tools.score_account(analyst_id, account_id)["data"]
    except graph_tools.ToolError as exc:
        raise HTTPException(404, str(exc))


class ScoresRequest(BaseModel):
    analyst_id: str
    account_ids: list[str] = Field(max_length=60)


@app.post("/api/scores")
def scores(req: ScoresRequest):
    """Risk scores for many accounts at once (colours the graph)."""
    check_analyst(req.analyst_id)
    return {"scores": graph_tools.score_many(req.analyst_id, req.account_ids)}


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    """Record the analyst's verdict and report how it moves the risk score of the finding's accounts."""
    check_analyst(req.analyst_id)
    mem = {f["finding_id"]: f for f in graph_tools.list_memory(req.analyst_id)["findings"]}
    if req.finding_id not in mem:
        raise HTTPException(404, "finding not found for this analyst")
    accounts = [e for e in mem[req.finding_id]["about"] if e.startswith("A-")][:12]
    before = {a: graph_tools.score_account(req.analyst_id, a)["data"]["final_score"] for a in accounts}
    try:
        graph_tools.record_feedback(req.analyst_id, req.finding_id, req.verdict)
    except graph_tools.ToolError as exc:
        raise HTTPException(400, str(exc))
    after = {a: graph_tools.score_account(req.analyst_id, a)["data"]["final_score"] for a in accounts}
    return {"finding_id": req.finding_id, "verdict": req.verdict,
            "score_changes": [{"account": a, "before": before[a], "after": after[a]} for a in accounts]}
