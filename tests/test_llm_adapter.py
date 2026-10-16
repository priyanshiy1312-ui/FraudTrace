"""The OpenAI-compatible adapter (Groq / Gemini / OpenRouter ...) against a mock server. No API key needed."""
import asyncio
import json

import httpx
import pytest

from backend import llm
from backend.llm import OpenAICompatClient, _to_openai_messages, load_config

SYSTEM = "you are a test"
TOOLS = [{"name": "get_account_profile", "description": "d",
          "input_schema": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]}}]


def completion(content=None, calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": a}}
                             for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg}]}


def client_with(handler, **kw):
    return OpenAICompatClient("https://example.test/v1", "k", transport=httpx.MockTransport(handler), **kw)


def create(client, **kw):
    args = dict(model="m", max_tokens=100, system=SYSTEM, tools=TOOLS, messages=[{"role": "user", "content": "hi"}])
    args.update(kw)
    return asyncio.run(client.messages.create(**args))


def test_request_translation_and_response_blocks():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json=completion("thinking", [("get_account_profile", '{"account_id":"A-0001"}')]))

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": [{"type": "text", "text": "looking"},
                                          {"type": "tool_use", "id": "t1", "name": "get_account_profile", "input": {"account_id": "A-0001"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}", "is_error": True}]},
    ]
    r = create(client_with(handler), messages=messages, tool_choice={"type": "tool", "name": "get_account_profile"})
    b = seen["body"]
    assert seen["url"] == "https://example.test/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert b["messages"][0] == {"role": "system", "content": SYSTEM}
    assert b["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"account_id": "A-0001"}'
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "ERROR: {}"}
    assert b["tools"][0]["function"]["parameters"]["required"] == ["account_id"]
    assert b["tool_choice"] == {"type": "function", "function": {"name": "get_account_profile"}}
    assert [x.type for x in r.content] == ["text", "tool_use"]
    assert r.content[1].input == {"account_id": "A-0001"} and r.content[1].id == "call_0"


def test_default_tool_choice_is_auto_and_bad_json_args_become_empty():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=completion(None, [("get_account_profile", "{not json")]))

    r = create(client_with(handler))
    assert seen["body"]["tool_choice"] == "auto"
    assert r.content[0].type == "tool_use" and r.content[0].input == {}


def test_old_tool_results_are_shortened_recent_kept():
    msgs = []
    for i in range(5):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "y" * 3000}]})
    tools = [m for m in _to_openai_messages(SYSTEM, msgs, keep_full_results=2) if m["role"] == "tool"]
    assert [len(t["content"]) > 1000 for t in tools] == [False, False, False, True, True]


def test_retries_on_429_and_tool_use_failed_then_succeeds(monkeypatch):
    async def no_sleep(_): pass
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    answers = iter([httpx.Response(429, text="slow down"),
                    httpx.Response(400, text='{"error":{"code":"tool_use_failed"}}'),
                    httpx.Response(200, json=completion("ok"))])
    r = create(client_with(lambda req: next(answers)))
    assert r.content[0].text == "ok"


def test_auth_error_is_not_retried_and_is_reported():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(RuntimeError, match="401"):
        create(client_with(handler))
    assert len(calls) == 1


def test_provider_auto_detection(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "GROQ_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert load_config().provider == "anthropic" and not load_config().configured
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    cfg = load_config()
    assert (cfg.provider, cfg.model, cfg.configured, cfg.small_context) == ("groq", "openai/gpt-oss-120b", True, True)
    assert cfg.base_url == "https://api.groq.com/openai/v1"
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    assert load_config().model == "llama-3.3-70b-versatile"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert load_config().provider == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    with pytest.raises(ValueError):
        load_config()


def test_full_investigation_through_the_adapter(analysts, gt):
    """Real MCP server + real graph, with a mock OpenAI-style 'model' that plays a short script."""
    from backend.agent import run_investigation
    ring = gt["ring"]["accounts"]
    script = iter([
        completion("Start at the flagged transaction.", [("get_transaction_links", '{"tx_id":"T-9001"}')]),
        completion("Score the suspect.", [("score_account", json.dumps({"account_id": ring[0]}))]),
        completion(None, [("save_finding", json.dumps({"entity_ids": ring[:3], "statement": "ring members share devices",
                                                       "confidence": 0.8, "evidence": ["get_transaction_links"]}))]),
        completion(None, [("submit_verdict", json.dumps({"verdict": "fraud_ring", "summary": "Ring.", "risk_score": 1,
                                                         "key_findings": ["shared devices"], "evidence_paths": [ring[:2]],
                                                         "confidence": 0.8, "limitations": ["synthetic"]}))]),
    ])
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=next(script))

    events = []

    async def emit(e): events.append(e)

    v = asyncio.run(run_investigation(analysts[0], "Which accounts are linked to flagged transaction T-9001?",
                                      emit, client_with(handler), "mock-model"))
    assert v["verdict"] == "fraud_ring" and v["risk_score_source"] == "score_account" and v["risk_score"] > 1
    names = {t["function"]["name"] for t in requests[0]["tools"]}
    assert "submit_verdict" in names and "get_transaction_links" in names
    assert all("analyst_id" not in t["function"]["parameters"].get("properties", {}) for t in requests[0]["tools"])
    assert [e["type"] for e in events][0] == "start" and events[-1]["type"] == "verdict"


def test_empty_env_vars_from_docker_compose_do_not_override_defaults(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
              "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    cfg = load_config()
    assert (cfg.provider, cfg.model, cfg.base_url) == ("groq", "openai/gpt-oss-120b", "https://api.groq.com/openai/v1")


def test_root_cause_unwraps_exception_groups():
    from backend.main import root_cause
    inner = RuntimeError("LLM request failed (401: invalid api key)")
    assert root_cause(ExceptionGroup("tg", [ExceptionGroup("inner", [inner])])) is inner
    assert root_cause(inner) is inner
