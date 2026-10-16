"""HTTP layer: SSE streaming, memory + feedback endpoints, and the analyst allow-list."""
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from backend import main
from mcp_server import tools as graph_tools

pytestmark = pytest.mark.usefixtures("live_db")


@pytest.fixture()
def client(monkeypatch, analysts):
    monkeypatch.setattr(main, "ANALYSTS", list(analysts))
    return TestClient(main.app)


def test_health_and_unknown_analyst(client):
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/memory/mallory").status_code == 404
    assert client.post("/api/investigate", json={"analyst_id": "mallory", "question": "hello there"}).status_code == 404
    assert client.post("/api/feedback", json={"analyst_id": "zz_test_a", "finding_id": "x; DROP", "verdict": "confirmed"}).status_code == 422


def test_sse_stream_and_feedback_changes_score(client, monkeypatch, analysts, gt):
    a, _ = analysts
    acc = gt["ring"]["accounts"][0]

    class Fake:
        def __init__(self):
            blocks = [
                NS(content=[NS(type="text", text="Look at the account."), NS(type="tool_use", id="1", name="get_account_profile", input={"account_id": acc})], stop_reason="tool_use"),
                NS(content=[NS(type="tool_use", id="2", name="save_finding", input={"entity_ids": [acc], "statement": "suspicious ring member", "confidence": 0.8, "evidence": ["profile"]})], stop_reason="tool_use"),
                NS(content=[NS(type="tool_use", id="3", name="submit_verdict", input={"verdict": "suspicious", "summary": "s", "risk_score": 50, "key_findings": [], "evidence_paths": [], "confidence": 0.7, "limitations": []})], stop_reason="tool_use"),
            ]
            self.script = blocks
            self.messages = NS(create=self.create)

        async def create(self, **kw):
            return self.script.pop(0)

    monkeypatch.setattr(main, "_client", Fake())
    with client.stream("POST", "/api/investigate", json={"analyst_id": a, "question": f"Look at {acc}"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "done" and "verdict" in types and "error" not in types

    mem = client.get(f"/api/memory/{a}").json()["findings"]
    assert len(mem) == 1
    base = client.get(f"/api/score/{a}/{acc}").json()["final_score"]
    fb = client.post("/api/feedback", json={"analyst_id": a, "finding_id": mem[0]["finding_id"], "verdict": "false_positive"}).json()
    assert fb["score_changes"][0]["before"] == base and fb["score_changes"][0]["after"] < base
    assert client.get(f"/api/score/{a}/{acc}").json()["final_score"] < base
