"""LLM client selection.

The agent loop (backend/agent.py) talks to a client that looks like the Anthropic SDK:
    await client.messages.create(model=, max_tokens=, system=, tools=, messages=, [tool_choice=])
and reads `response.content` blocks of type "text" / "tool_use".

`OpenAICompatClient` gives the same interface on top of any OpenAI-compatible chat-completions API
(Groq, Google Gemini's OpenAI endpoint, OpenRouter, GitHub Models, Ollama, ...), so the free providers
work without touching the agent logic.

Environment:
    LLM_PROVIDER   groq | gemini | openrouter | openai_compat | anthropic   (auto-detected from keys if unset)
    LLM_MODEL      overrides the provider's default model
    LLM_BASE_URL / LLM_API_KEY   for openai_compat (or to override a preset)
    GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from types import SimpleNamespace as NS

import httpx

PRESETS = {
    #            base url,                                                     key env,              default model
    "groq":       ("https://api.groq.com/openai/v1",                            "GROQ_API_KEY",       "openai/gpt-oss-120b"),
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai",   "GEMINI_API_KEY",     "gemini-2.5-flash"),
    "openrouter": ("https://openrouter.ai/api/v1",                              "OPENROUTER_API_KEY", "openai/gpt-oss-120b:free"),
}
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    small_context: bool        # True for free tiers with tight token-per-minute limits -> trim tool results

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def load_config() -> LLMConfig:
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not provider:                                   # auto-detect: first key that is set wins
        provider = next((p for p, (_, key_env, _) in PRESETS.items() if os.getenv(key_env)), "")
        provider = provider or ("openai_compat" if os.getenv("LLM_API_KEY") else "anthropic")
    if provider == "anthropic":
        return LLMConfig("anthropic", os.getenv("LLM_MODEL") or os.getenv("ANTHROPIC_MODEL") or ANTHROPIC_DEFAULT_MODEL,
                         os.getenv("ANTHROPIC_API_KEY"), None, small_context=False)
    if provider in PRESETS:
        base, key_env, model = PRESETS[provider]
        return LLMConfig(provider, os.getenv("LLM_MODEL") or model, os.getenv("LLM_API_KEY") or os.getenv(key_env),
                         os.getenv("LLM_BASE_URL") or base, small_context=True)
    if provider == "openai_compat":
        return LLMConfig(provider, os.getenv("LLM_MODEL") or "", os.getenv("LLM_API_KEY"),
                         os.getenv("LLM_BASE_URL") or "", small_context=True)
    raise ValueError(f"unknown LLM_PROVIDER '{provider}'")


def build_client(cfg: LLMConfig):
    if cfg.provider == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=cfg.api_key)
    if not cfg.base_url or not cfg.model:
        raise ValueError("openai_compat needs LLM_BASE_URL and LLM_MODEL")
    return OpenAICompatClient(cfg.base_url, cfg.api_key or "", keep_full_results=3)


# ----------------------------------------------------------------------------- format translation
def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in content if b.get("type") == "text")


def _to_openai_messages(system: str, messages: list[dict], keep_full_results: int, old_chars: int = 500) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": _text_of(content) if not isinstance(content, str) else content}
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in (content if isinstance(content, list) else []) if b.get("type") == "tool_use"]
            if calls:
                msg["tool_calls"] = calls
                msg["content"] = msg["content"] or None
            out.append(msg)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            for b in content:
                if b.get("type") == "tool_result":
                    body = b["content"] if isinstance(b["content"], str) else _text_of(b["content"])
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("ERROR: " + body) if b.get("is_error") else body})
            text = _text_of([b for b in content if b.get("type") == "text"])
            if text:
                out.append({"role": "user", "content": text})
    # Save tokens: only the most recent tool results stay in full, older ones are shortened.
    tool_idx = [i for i, m in enumerate(out) if m["role"] == "tool"]
    for i in tool_idx[:-keep_full_results] if keep_full_results else tool_idx:
        body = out[i]["content"]
        if len(body) > old_chars:
            out[i]["content"] = body[:old_chars] + " ...[older result shortened]"
    return out


def _from_openai(data: dict):
    msg = data["choices"][0]["message"]
    blocks = []
    if msg.get("content"):
        blocks.append(NS(type="text", text=msg["content"]))
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(NS(type="tool_use", id=tc["id"], name=tc["function"]["name"], input=args))
    return NS(content=blocks, stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")


# ----------------------------------------------------------------------------- the client
class OpenAICompatClient:
    """Anthropic-shaped `.messages.create()` over an OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, base_url: str, api_key: str, keep_full_results: int = 3, retries: int = 4,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 120.0):
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.keep_full_results, self.retries = keep_full_results, retries
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.messages = NS(create=self._create)

    async def _create(self, *, model: str, max_tokens: int, system: str, tools: list[dict], messages: list[dict],
                      tool_choice: dict | None = None, **_ignored):
        body = {"model": model, "max_tokens": max_tokens,
                "messages": _to_openai_messages(system, messages, self.keep_full_results),
                "tools": _to_openai_tools(tools)}
        body["tool_choice"] = ({"type": "function", "function": {"name": tool_choice["name"]}}
                               if tool_choice and tool_choice.get("type") == "tool" else "auto")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = ""
        for attempt in range(self.retries + 1):
            r = await self._http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if r.status_code == 200:
                return _from_openai(r.json())
            last = f"{r.status_code}: {r.text[:300]}"
            transient = r.status_code in (429, 500, 502, 503, 504) or \
                (r.status_code == 400 and "tool_use_failed" in r.text)     # model emitted a malformed tool call
            if not transient or attempt == self.retries:
                break
            wait = float(r.headers.get("retry-after", 0) or 0) or min(2 ** attempt * 2, 20)
            await asyncio.sleep(min(wait, 30))
        raise RuntimeError(f"LLM request failed ({last})")
