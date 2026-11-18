"""v5 reply writer: a model turns verified facts into a natural reply; the harness checks it before it is sent.

The investigation (checks, fixes, re-checks, tickets) is done by code in playbooks.py. When that is finished, this
module may hand the model three things only: the employee's words, the FACTS the code verified, and a short passage
from the company knowledge base. The model writes the wording. Then validate() rejects any reply that states an action
that is not in the facts, asks for a password or code, adds a link, drops a ticket number, talks about "the employee"
instead of "you", or leaks reasoning. A rejected, slow or failed reply is replaced by the deterministic template, so the
employee always gets a correct answer quickly.

Configuration (environment variables):
  REPLY_PROVIDER  none (default, templates only) | bedrock | anthropic | ollama
  REPLY_MODEL     model id. Bedrock: copy the id from the Bedrock console model catalog. Anthropic: claude-haiku-5-5.
  REPLY_TIMEOUT   seconds to wait for the model (default 25)
  AWS_REGION      Bedrock region (default us-east-1); credentials come from the standard AWS environment/profile.
"""
import json
import logging
import os
import re
import time

import httpx
import progress
from llm import clean_text
from retrieval import search_knowledge_base

logger = logging.getLogger("support")

SYSTEM = """You write replies for the IT support agent of Corvane Payments, a company with about 10 employees.
The system has already investigated the problem. You only put the result into clear, warm, natural language.

Rules:
1. Speak to the employee as "you" and call yourself "I". Never write "the employee" or the employee's name in the third person.
2. FACTS lists everything that really happened. State an action as done ONLY if it is in FACTS. Never claim you fixed,
   restarted, reset, installed or checked anything that FACTS does not list. If FACTS says nothing was changed, say so plainly.
3. Keep every ticket number (like #12), every figure and every question the DRAFT asks the employee. Do not invent numbers,
   times, names or steps. You may use the COMPANY KNOWLEDGE for at most one short practical tip, and only if it fits.
4. Never ask for a password, code or any secret. Never include a link or web address that is not in the DRAFT.
5. Plain text only: short paragraphs, no headings, no tables, no emoji, no em dashes. At most 120 words unless the DRAFT needs more.
6. Do not show your reasoning. Output only the reply that the employee will read."""

_CLAIM_VERBS = ("fixed|resolved|repaired|restarted|reset|cleared|renewed|reinstalled|installed|archived|unlocked|updated|"
                "flushed|enabled|re-enabled|rebuilt|terminated|deployed|issued|removed|revoked|signed|raised|created|"
                "opened|sent|checked|ran|moved|pushed|released|redeployed|re-deployed|rechecked|re-checked")
_CLAIM = re.compile(rf"\b(?:I|we)(?:'ve| have)?\s+(?:[\w'-]+\s+){{0,2}}?({_CLAIM_VERBS})\b", re.I)
_VERB = re.compile(rf"\b({_CLAIM_VERBS})\b", re.I)
_SECRET = re.compile(r"\b(send|share|tell|give|type|enter|paste|provide|reply with|text)\b[^.?!\n]{0,30}\b(password|passcode|pin|mfa code|verification code|one-time code|security code)\b", re.I)
_NEGATION = re.compile(r"(never|don'?t|do not|won'?t|not|no one|nobody|without)\W+(?:\w+\W+){0,4}$", re.I)
_URL = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|ai)\b[\S]*", re.I)

_override = None          # tests install a stand-in: fn(system, user) -> str
_client = None            # cached Bedrock client
_down_until = 0.0         # circuit breaker: skip the model briefly after a connection failure
_failures = 0


class ReplyError(Exception):
    pass


def set_reply_model(fn) -> None:
    global _override, _down_until, _failures
    _override, _down_until, _failures = fn, 0.0, 0


def provider() -> str:
    return os.getenv("REPLY_PROVIDER", "none").strip().lower()


def enabled() -> bool:
    return _override is not None or provider() != "none"


def model_id() -> str:
    return os.getenv("REPLY_MODEL", "")


def timeout() -> float:
    return float(os.getenv("REPLY_TIMEOUT", "25"))


# ------------------------------------------------------------------ providers
def complete(system: str, user: str, max_tokens: int = 450, model: str | None = None) -> str:
    """One model call through the configured provider. Raises ReplyError on any failure."""
    if _override is not None:
        return _override(system, user)
    kind, mid = provider(), model or model_id()
    try:
        if kind == "bedrock":
            return _bedrock(system, user, max_tokens, mid)
        if kind == "anthropic":
            return _anthropic(system, user, max_tokens, mid or "claude-haiku-5-5")
        if kind == "ollama":
            return _ollama(system, user, max_tokens, mid or os.getenv("AGENT_MODEL") or "llama3.2:3b")
    except ReplyError:
        raise
    except Exception as error:  # network, credentials, throttling, unknown model
        raise ReplyError(f"{type(error).__name__}: {str(error)[:160]}") from error
    raise ReplyError(f"unknown REPLY_PROVIDER '{kind}'")


def _bedrock(system: str, user: str, max_tokens: int, mid: str) -> str:
    global _client
    if not mid:
        raise ReplyError("REPLY_MODEL is not set (copy the model id from the Bedrock console)")
    import boto3
    from botocore.config import Config
    if _client is None:
        _client = boto3.client("bedrock-runtime",
                               region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1",
                               config=Config(read_timeout=timeout(), connect_timeout=5, retries={"max_attempts": 1}))
    response = _client.converse(modelId=mid, system=[{"text": system}],
                                messages=[{"role": "user", "content": [{"text": user}]}],
                                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2})
    return "".join(part.get("text", "") for part in response["output"]["message"]["content"])


def _anthropic(system: str, user: str, max_tokens: int, mid: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ReplyError("ANTHROPIC_API_KEY is not set")
    with httpx.Client(timeout=httpx.Timeout(timeout(), connect=5)) as client:
        response = client.post("https://api.anthropic.com/v1/messages",
                               headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                               json={"model": mid, "max_tokens": max_tokens, "temperature": 0.2, "system": system,
                                     "messages": [{"role": "user", "content": user}]})
        response.raise_for_status()
        return "".join(part.get("text", "") for part in response.json().get("content", []))


def _ollama(system: str, user: str, max_tokens: int, mid: str) -> str:
    payload = {"model": mid, "stream": False, "keep_alive": "30m",
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "options": {"temperature": 0.2, "num_predict": max_tokens, "num_ctx": 3072}}
    if mid.lower().startswith("qwen3"):
        payload["think"] = False
    url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(timeout(), connect=5), trust_env=False) as client:
        response = client.post(f"{url}/api/chat", json=payload)
        response.raise_for_status()
        return response.json()["message"]["content"]


# ------------------------------------------------------------------ facts and knowledge
def build_facts(ctx) -> dict:
    checks, actions, tickets = [], [], []
    for item in ctx.tool_results:
        tool, result = item["tool"], item["result"]
        if tool.startswith(("diagnose_", "verify_")) and "checks" in result:
            checks = result["checks"]
        elif tool == "escalate_support_issue":
            continue
        elif isinstance(result, dict) and "ok" in result and "detail" in result:
            actions.append({"done": bool(result["ok"]), "what": result["detail"]})
    for ticket in ctx.created_tickets:
        tickets.append({"id": ticket["id"], "complexity": ticket.get("complexity"),
                        "target": ticket.get("resolution_estimate")})
    failing = [f"{c['check']}: {c['detail']}" for c in checks if not c["ok"]]
    return {"outcome": ctx.outcome,
            "checks_run": len(checks), "checks_passing": len(checks) - len(failing), "checks_failing": failing,
            "actions_done_this_turn": [a["what"] for a in actions if a["done"]],
            "actions_rejected_this_turn": [a["what"] for a in actions if not a["done"]],
            "tickets": tickets,
            "nothing_was_changed": not any(a["done"] for a in actions),
            "waiting_for_employee": ctx.outcome in {"needs_clarification", "fixed_pending_confirmation"}}


def knowledge_for(ctx) -> tuple[str, list[str]]:
    reports = [r for issue in ctx.state.get("issues", {}).values() for r in issue.get("reports", [])[-2:]]
    query = " ".join(reports[-3:] + [ctx.text])[:600]
    try:
        results = search_knowledge_base(query)["results"]
    except Exception:
        return "", []
    if not results or results[0]["score"] < 2:
        return "", []
    top = results[0]
    body = "\n".join(line for line in top["content"].splitlines()
                     if line.strip() and not line.startswith(("Source ID", "Audience", "Environment", "#")))
    return body[:900], [top["source"]]


def build_prompt(ctx, facts: dict, knowledge: str) -> str:
    first = (ctx.employee.get("name") or "").split(" ")[0]
    greeting = (f"This is the first message: start with a short greeting using the first name ({first}).\n"
                if getattr(ctx, "first_turn", True) else
                "The conversation is already under way: do NOT greet and do NOT use the employee's name; "
                "carry on directly.\n")
    return (greeting + f"EMPLOYEE FIRST NAME: {first}\n"
            f"EMPLOYEE MESSAGE: {ctx.text[:600]}\n\n"
            f"FACTS (verified by the system):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"DRAFT (correct, but stiff; same facts, keep what it asks and every ticket number):\n{ctx.answer}\n\n"
            f"COMPANY KNOWLEDGE (optional):\n{knowledge or '(none relevant)'}\n\n"
            "Write the reply to the employee now.")


# ------------------------------------------------------------------ validation
_LEADING_GREETING = re.compile(r"^(?:hi|hello|hey)\b[^\n.!?]{0,30}[,!.]\s*", re.I)
_INVITES_ANSWER = re.compile(
    r"\b(let me know|tell me|reply|respond|get back to me|message me|write back|confirm (?:whether|if))\b", re.I)
_PRODUCT_NUMBERS = re.compile(
    r"\b(?:Microsoft|Office|M)\s*365\b|\b(?:Windows|Win)\s*(?:10|11)\b|\b(?:Office|Outlook|Excel|Word)\s*(?:2016|2019|2021|2024)\b",
    re.I,
)


def validate(reply: str, draft: str, facts: dict) -> str | None:
    """Return None if the reply may be sent, otherwise the reason it was rejected."""
    if len(reply) < 20:
        return "reply is empty or too short"
    if len(reply) > max(1800, int(len(draft) * 1.5)):
        return "reply is too long"
    if "<think>" in reply or "</think>" in reply:
        return "reasoning leaked into the reply"
    if re.search(r"\bthe employee\b|\bthe user\b", reply, re.I):
        return "talks about the employee instead of to them"
    for match in _SECRET.finditer(reply):
        if not _NEGATION.search(reply[:match.start()][-40:]):
            return "asks for a password or code"
    allowed_urls = {u.lower().rstrip(".,)") for u in _URL.findall(draft)}
    for url in _URL.findall(reply):
        if url.lower().rstrip(".,)") not in allowed_urls:
            return f"contains a link that is not in the draft ({url[:40]})"
    draft_ids, reply_ids = set(re.findall(r"#\d+", draft)), set(re.findall(r"#\d+", reply))
    if reply_ids - draft_ids:
        return "mentions a ticket number that does not exist"
    if draft_ids - reply_ids:
        return "dropped the ticket number"
    known_numbers = set(re.findall(r"\d+%?", draft + json.dumps(facts)))
    # product names contain numbers (Microsoft 365, Windows 11, Office 2021); they are not claims
    scrubbed = _PRODUCT_NUMBERS.sub(" ", reply)
    extra = {n for n in re.findall(r"\d+%?", scrubbed) if n not in known_numbers and len(n.rstrip("%")) > 1}
    if extra:
        return "contains a number that is not in the facts: " + ", ".join(sorted(extra))
    allowed_verbs = {v.lower() for v in _VERB.findall(draft + " " + json.dumps(facts))}
    claimed = {m.lower() for m in _CLAIM.findall(reply)}
    if claimed - allowed_verbs:
        return "claims an action that is not in the facts: " + ", ".join(sorted(claimed - allowed_verbs))
    if "?" in draft and "?" not in reply and not _INVITES_ANSWER.search(reply):
        return "dropped the question the draft asks"
    return None


# ------------------------------------------------------------------ entry point
def polish(ctx) -> dict:
    """Rewrite ctx.answer with the model if allowed and valid. Always returns metadata; never raises."""
    global _down_until, _failures
    meta = {"source": "template"}
    if (not enabled() or getattr(ctx, "verbatim", False) or ctx.secret_links
            or ctx.outcome == "tool_error" or not ctx.answer):
        return meta
    if time.time() < _down_until:
        meta["fallback"] = "model recently unreachable; using the built-in reply"
        return meta
    employee_id = ctx.employee["id"]
    started = time.monotonic()
    progress.add(employee_id, "Writing the reply")
    try:
        facts = build_facts(ctx)
        knowledge, sources = knowledge_for(ctx)
        raw = complete(SYSTEM, build_prompt(ctx, facts, knowledge))
        reply = clean_text(raw).strip()
        if not getattr(ctx, "first_turn", True):
            reply = _LEADING_GREETING.sub("", reply).lstrip()
        reason = validate(reply, ctx.answer, facts)
        if reason:
            raise ReplyError("rejected by the harness: " + reason)
        ctx.answer = reply
        ctx.sources.update(sources)
        _failures = 0
        meta.update(source="model", model=model_id() or provider())
    except ReplyError as error:
        meta["fallback"] = str(error)
        if not str(error).startswith("rejected"):
            _failures += 1
            if _failures >= 2:
                _down_until = time.time() + 60
        progress.add(employee_id, "Using the built-in reply (" + str(error)[:70] + ")")
        logger.warning('{"event": "reply_fallback", "reason": %s}', json.dumps(str(error)))
    meta["ms"] = round((time.monotonic() - started) * 1000)
    return meta
