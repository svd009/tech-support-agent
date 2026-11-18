"""Model providers for the agent loop.

The harness talks to every model through one small interface:

    llm.chat(messages, tools) -> LLMReply(content, tool_calls, usage)

Messages use one neutral format (below). Each provider converts it to its own wire format.

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Providers:
  ollama     local model through Ollama's /api/chat with tool calling (AGENT_MODEL, default qwen3:4b)
  anthropic  Claude through the Messages API with tool use (needs ANTHROPIC_API_KEY; default claude-haiku-5-5)
"""
import json
import os
import re
from dataclasses import dataclass, field

import httpx

import config


class LLMError(Exception):
    """The model could not be reached or returned something unusable."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    reasoning_leaked: bool = False  # the model wrote its private reasoning into the reply (it was removed)


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


def clean_text(text: str) -> str:
    """Remove a reasoning model's private thinking from a reply.

    Thinking models may emit "<think>...</think>answer", or only "...</think>answer" (the opening tag is part of
    their prompt template), or get cut off mid-thought. Only the text after the last closing tag is the answer;
    an unfinished thought is discarded entirely so it can never reach the employee.
    """
    text = text or ""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if "<think>" in text:
        text = ""
    return text.strip()


def leaked(text: str) -> bool:
    return "</think>" in (text or "") or "<think>" in (text or "")


class OllamaLLM:
    name = "ollama"

    def __init__(self, model: str | None = None):
        self.model = model or config.AGENT_MODEL or "qwen3:4b"

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        payload = {
            "model": self.model,
            "messages": [self._message(m) for m in messages],
            "tools": [{"type": "function", "function": t} for t in tools],
            "stream": False,
            "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
            "options": {"num_ctx": config.AGENT_NUM_CTX, "temperature": 0.2, "num_predict": 450},
        }
        think = os.getenv("OLLAMA_THINK", "").lower()
        if think in {"true", "false"}:
            payload["think"] = think == "true"
        elif self.model.lower().startswith("qwen3"):
            payload["think"] = False  # faster on CPU, and tool calls are more reliable without long reasoning
        try:
            with httpx.Client(timeout=httpx.Timeout(config.AGENT_MODEL_TIMEOUT, connect=5), trust_env=False) as client:
                response = client.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama is not reachable at {config.OLLAMA_URL} ({type(exc).__name__}).") from exc
        if response.status_code == 404:
            raise LLMError(f"Ollama does not have the model '{self.model}'. Run: ollama pull {self.model}")
        if response.status_code >= 400:
            raise LLMError(f"Ollama returned HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        message = body.get("message", {})
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function", {})
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            calls.append(ToolCall(f"call_{index}", function.get("name", ""), arguments if isinstance(arguments, dict) else {}))
        raw = message.get("content", "")
        text = clean_text(raw)
        if body.get("done_reason") == "length" and not calls and "</think>" not in raw:
            text = ""  # cut off before finishing: never show a half-written reply
        return LLMReply(text, calls, {"input_tokens": body.get("prompt_eval_count", 0), "output_tokens": body.get("eval_count", 0)},
                        reasoning_leaked=leaked(raw) or bool(message.get("thinking")))

    @staticmethod
    def _message(m: dict) -> dict:
        if m["role"] == "assistant" and m.get("tool_calls"):
            return {"role": "assistant", "content": m.get("content", ""),
                    "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]}
        if m["role"] == "tool":
            return {"role": "tool", "tool_name": m["name"], "content": m["content"]}
        return {"role": m["role"], "content": m.get("content", "")}


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        self.model = model or config.AGENT_MODEL or "claude-haiku-5-5"
        self.key = os.getenv("ANTHROPIC_API_KEY", "")

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        if not self.key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        payload = {
            "model": self.model, "max_tokens": 900, "temperature": 0.2, "system": system,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
            "messages": self._convert([m for m in messages if m["role"] != "system"]),
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(config.AGENT_MODEL_TIMEOUT, connect=10)) as client:
                response = client.post("https://api.anthropic.com/v1/messages", json=payload, headers={
                    "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        except httpx.HTTPError as exc:
            raise LLMError(f"Anthropic API is not reachable ({type(exc).__name__}).") from exc
        if response.status_code >= 400:
            raise LLMError(f"Anthropic API returned HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        text, calls = [], []
        for block in body.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
        usage = body.get("usage", {})
        return LLMReply(clean_text("\n".join(text)), calls,
                        {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)})

    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no reply)"}]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return out


_OVERRIDE = None  # tests and offline evaluation install a stand-in model here


def set_llm(llm) -> None:
    global _OVERRIDE
    _OVERRIDE = llm


def get_llm():
    if _OVERRIDE is not None:
        return _OVERRIDE
    provider = os.getenv("LLM_PROVIDER", config.LLM_PROVIDER).lower()
    if provider == "anthropic":
        return AnthropicLLM()
    if provider == "ollama":
        return OllamaLLM()
    raise LLMError(f"Unknown LLM_PROVIDER '{provider}' (use ollama or anthropic).")
