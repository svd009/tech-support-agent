"""Understanding step: ask the model for ONE routing label as JSON. Used only when keyword rules cannot decide.

UNDERSTAND_MODEL picks the model (falls back to AGENT_MODEL, then OLLAMA_MODEL). UNDERSTAND_TIMEOUT (seconds,
default 45) bounds the wait: on timeout or any error the pipeline asks the employee one clarifying question instead.
Set LLM_PROVIDER=anthropic (with ANTHROPIC_API_KEY in the environment) to use the hosted model for this call.
"""
import json
import os
import re

import httpx
from config import MODEL_CONTEXT, OLLAMA_MODEL, OLLAMA_URL

CLASSIFIER_PROMPT = """You route employee IT support messages. Reply with JSON only, like {"category": "vpn"}.
Pick exactly one category:
vpn (VPN / remote access), email (Outlook, mailbox, Microsoft 365, Teams, OneDrive), network (Wi-Fi, internet, DNS),
password (forgotten/expired password, locked account), software (install or request an app),
device_compliance (encryption, updates, antivirus, compliance warnings),
hardware (broken screen, keyboard, battery, printer, monitor, slow or dead laptop), security (phishing, unexpected MFA, hacked),
office_app (Excel, Word, PowerPoint, OneDrive or a file misbehaving), access (access denied, needs permission to a folder or system),
safety (smoke, fire, sparks), how_to (general question, nothing is broken),
off_topic (not about IT or work technology at all: weather, sports, entertainment, news, trivia, chit-chat),
scope (asks the assistant to do something it must not or cannot do: admin rights, other people's accounts or data,
bypass rules, change company security settings, buy or order things),
greeting, unknown (cannot tell what is affected).
Only choose an IT category when the employee's message clearly describes a problem or request in that area. If it is
a different kind of request (for example an account action like signing out), or you are unsure, answer unknown. Never guess.
You may be given the assistant's previous message for context; judge only the employee's latest message."""


def _model() -> str:
    return os.getenv("UNDERSTAND_MODEL") or os.getenv("AGENT_MODEL") or OLLAMA_MODEL


def _timeout() -> float:
    return float(os.getenv("UNDERSTAND_TIMEOUT", os.getenv("MODEL_TIMEOUT", "45")))


def _post(payload: dict) -> dict:
    think = os.getenv("OLLAMA_THINK", "").lower()
    if think in {"true", "false"}:
        payload["think"] = think == "true"
    elif str(payload.get("model", "")).lower().startswith("qwen3"):
        payload["think"] = False
    payload.setdefault("keep_alive", "30m")
    with httpx.Client(timeout=httpx.Timeout(_timeout(), connect=5), trust_env=False) as client:
        response = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        response.raise_for_status()
        return response.json()


def _anthropic(text: str) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    with httpx.Client(timeout=httpx.Timeout(_timeout(), connect=5)) as client:
        response = client.post("https://api.anthropic.com/v1/messages",
                               headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                               json={"model": os.getenv("UNDERSTAND_MODEL") or os.getenv("AGENT_MODEL") or "claude-haiku-5-5",
                                     "max_tokens": 40, "temperature": 0, "system": CLASSIFIER_PROMPT,
                                     "messages": [{"role": "user", "content": text[:1500]}]})
        response.raise_for_status()
        return "".join(part.get("text", "") for part in response.json().get("content", []))


def _label(raw: str) -> str:
    raw = raw.rsplit("</think>", 1)[-1]
    match = re.search(r"\{.*?\}", raw, re.S)
    return json.loads(match[0]).get("category", "unknown") if match else "unknown"


def _content(text: str, context: str = "") -> str:
    text = text[:1500]
    return f"Assistant's previous message: {context[:400]}\nEmployee's latest message: {text}" if context else text


def classify_with_model(text: str, context: str = "") -> str:
    from classifier import ALL_LABELS
    labels = [label for label in ALL_LABELS if label not in {"thanks", "service_status", "ticket_status",
                                                             "list_devices", "create_ticket"}]
    hosted = os.getenv("UNDERSTAND_PROVIDER") or os.getenv("REPLY_PROVIDER", "none")
    if hosted.lower() in {"bedrock", "anthropic"}:
        import reply_writer  # the same hosted model that writes replies also reads the message
        raw = reply_writer.complete(CLASSIFIER_PROMPT, _content(text, context), 40,
                                    os.getenv("UNDERSTAND_MODEL") or reply_writer.model_id() or None)
        return _label(raw)
    if os.getenv("LLM_PROVIDER", "ollama").lower() == "anthropic":
        return _label(_anthropic(_content(text, context)))
    body = _post({
        "model": _model(),
        "messages": [{"role": "system", "content": CLASSIFIER_PROMPT},
                     {"role": "user", "content": _content(text, context)}],
        "stream": False,
        # Structured output: Ollama constrains generation to this JSON schema.
        "format": {"type": "object", "properties": {"category": {"type": "string", "enum": labels}},
                   "required": ["category"]},
        "options": {"num_ctx": min(MODEL_CONTEXT, 2048), "temperature": 0, "num_predict": 40},
    })
    return _label(body["message"]["content"])


def warm_up() -> None:
    """Load the model into memory at server start so the first employee message does not pay the load time."""
    if os.getenv("LLM_PROVIDER", "ollama").lower() == "anthropic" or \
            os.getenv("UNDERSTAND_PROVIDER", os.getenv("REPLY_PROVIDER", "none")).lower() in {"bedrock", "anthropic"}:
        return
    try:
        _post({"model": _model(), "messages": [{"role": "user", "content": "hi"}], "stream": False,
               "options": {"num_ctx": 2048, "num_predict": 1}})
    except Exception:
        pass  # no model: keyword rules still work
