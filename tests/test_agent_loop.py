"""End-to-end test of the agent loop with a scripted fake model (no API key needed).

It exercises the real MCP server over stdio and the real graph: tool discovery, identity injection,
memory prefetch, the evidence gate, actions, memory writes and the final verdict.
"""
import asyncio
from types import SimpleNamespace as NS

import pytest

from backend.agent import run_investigation, to_model_tools, HIDDEN_ARGS
from mcp_server import tools as graph_tools

pytestmark = pytest.mark.usefixtures("live_db")


def text(t): return NS(type="text", text=t)
def use(i, name, **inp): return NS(type="tool_use", id=f"tu{i}", name=name, input=inp)
def reply(*blocks): return NS(content=list(blocks), stop_reason="tool_use")


class FakeClient:
    """Plays a fixed script; also records what the 'model' was shown."""
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = NS(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def run(analyst, question, script):
    events = []
    async def emit(e): events.append(e)
    client = FakeClient(script)
    verdict = asyncio.run(run_investigation(analyst, question, emit, client, "fake-model"))
    return verdict, events, client


def test_full_investigation_then_second_session_uses_memory(analysts, gt):
    a, b = analysts
    ring = gt["ring"]["accounts"]
    inv_script = [
        reply(text("Start from the flagged transaction."), use(1, "get_transaction_links", tx_id="T-9001")),
        reply(text("Cluster the linked accounts."), use(2, "detect_communities")),
        reply(text("Check for a money loop."), use(3, "detect_cycles", account_id=ring[0], max_len=12, min_repeats=3)),
        reply(text("Score the suspect."), use(4, "score_account", account_id=ring[0],
                                              analyst_id="alice")),   # model tries to pick an analyst: must be ignored
        reply(text("Evidence is strong; act."),
              use(5, "flag_accounts", account_ids=ring, reason="ring", confidence=0.9, evidence=["detect_cycles: 12-hop loop"]),
              use(6, "create_case", account_ids=ring, summary="ring case", evidence=["detect_communities"])),
        reply(text("Remember this."), use(7, "save_finding", entity_ids=ring, statement="12-account ring sharing 3 devices",
                                          confidence=0.9, evidence=["detect_communities"])),
        reply(use(8, "submit_verdict", verdict="fraud_ring", summary="Ring.", risk_score=1, key_findings=["loop"],
                  evidence_paths=[ring[:3]], confidence=0.9, limitations=["synthetic data"])),
    ]
    v, events, client = run(a, "Which accounts are linked to flagged transaction T-9001?", inv_script)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and "tool_call" in kinds and kinds[-1] == "verdict"
    assert v["verdict"] == "fraud_ring" and v["risk_score_source"] == "score_account" and v["risk_score"] >= 60
    assert len(v["actions_taken"]) == 2 and v["queries_used"] and v["tool_calls"] >= 7
    # identity injection: the model's analyst_id was stripped from what it was shown and overridden on the call
    shown = {t["name"]: t for t in client.calls[0]["tools"]}
    assert not (HIDDEN_ARGS & set(shown["score_account"]["input_schema"]["properties"]))
    assert graph_tools.list_memory(a)["findings"]      # saved under the REAL analyst, not the one the model tried to name
    # memory persisted for A only
    assert graph_tools.list_memory(b)["findings"] == []

    # ---- new session: the same account is mentioned -> memory is prefetched and shown to the model
    script2 = [reply(use(1, "get_account_profile", account_id=ring[0])),
               reply(use(2, "submit_verdict", verdict="fraud_ring", summary="Known.", risk_score=0, key_findings=[],
                         evidence_paths=[], confidence=0.8, limitations=[]))]
    v2, events2, client2 = run(a, f"What do we know about {ring[0]}?", script2)
    assert v2["memory_used"] and any(e["type"] == "memory" for e in events2)
    assert "12-account ring" in client2.calls[0]["messages"][0]["content"]
    # the other analyst gets nothing for the same question
    v3, _, client3 = run(b, f"What do we know about {ring[0]}?", [reply(use(1, "submit_verdict", verdict="inconclusive", summary="-",
                         risk_score=0, key_findings=[], evidence_paths=[], confidence=0.1, limitations=[]))])
    assert not v3["memory_used"] and "nothing remembered yet" in client3.calls[0]["messages"][0]["content"]


def test_actions_are_refused_before_any_evidence(analysts, gt):
    a, _ = analysts
    ring = gt["ring"]["accounts"]
    script = [reply(use(1, "flag_accounts", account_ids=ring[:2], reason="x", confidence=0.9, evidence=["guess"])),
              reply(use(2, "submit_verdict", verdict="inconclusive", summary="-", risk_score=0, key_findings=[],
                        evidence_paths=[], confidence=0.1, limitations=[]))]
    v, events, _ = run(a, "Flag these accounts", script)
    res = next(e for e in events if e["type"] == "tool_result" and e["name"] == "flag_accounts")
    assert res["ok"] is False and "no graph evidence" in res["summary"]
    assert v["actions_taken"] == []
