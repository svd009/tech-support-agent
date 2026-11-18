"""The v3 agent loop: the model reasons and picks tools; the harness owns everything that must be right.

Model:   reads the employee's own words, decides which checks to run, interprets results, asks a
         clarifying question, writes the reply.
Harness: injects identity and session facts, validates and runs tools (agent_tools.py), verifies every fix,
         budgets steps, and after the model finishes applies safety nets so the outcome can't depend on
         the model remembering to do the right thing:
           - a diagnosed fault that only a human can fix always gets a ticket (unless the employee declined)
           - an expired password always gets the one-time reset link
           - "it's fixed" is corrected if the latest checks still fail
           - no password/MFA request and no reset link can ever come from model text
           - unsafe-equipment reports always get the safety instruction and a hardware ticket
"""
import os
import re
from datetime import datetime

import config
import playbooks as pb
import progress
from agent_tools import (AREAS, AgentContext, execute, failing, human_needed, observation_text, peek, tool_schemas)
from classifier import followup_signal, keyword_intent
from it_systems import get_state
from llm import LLMError, ToolCall, clean_text
from sessions import get_session
from tickets import list_tickets

SYSTEM_PROMPT = """You are the IT support agent for one employee. Solve problems yourself with your tools; use a human technician only when truly needed.
1. Understand the report in the employee's own words. If it is too vague to act on, ask ONE specific question and stop.
2. Investigate: run_diagnostics for every area that could be involved (a VPN problem may be network, password or certificate). check_service_status tells outages from personal problems.
3. Fix: apply_fix for each failed check that names a fix (several calls in one step are fine). The system re-checks; read the verification.
4. Passwords: send_password_reset_link. Never ask for or repeat passwords, codes or links.
5. If checks pass but the problem remains: search_knowledge and/or ask for the exact error, give steps, ask them to try.
6. create_ticket only when a human is needed: technician-owned failures, rejected fixes, hardware, approvals, outages, or after the steps above failed.
7. Never say something is fixed unless the latest verification passes. The system adds ticket numbers; never invent them.
Voice: speak directly to the employee as "you". Never talk about them in third person and never show your reasoning or notes. Ticket titles and reasons are for the technician: write them in third person ("the employee ...").
Reply briefly in plain language: what you found, what you did, what you suggest next. No tool names."""

PREFETCH_AREA = {"vpn": "vpn", "email": "email", "network": "network", "device_compliance": "device", "password": "account"}

_CLAIM = re.compile(r"\b(fixed|resolved|should (now )?(work|be working)|all set|good to go|is working now|works now)\b", re.I)
_ACTION_CLAIM = re.compile(
    r"\b(i|we)(?:'ve| have)?\s+(?:just\s+|now\s+)?(fixed|resolved|restarted|reset|cleared|renewed|unlocked|repaired|re-?enabled|flushed|updated|reinstalled|cleaned|rebuilt)\b"
    r"|\b(is|are|now)\s+(now\s+)?(responding|working again|fixed|resolved)\b|\b(should|will)\s+(now\s+)?(work|be working)\b", re.I)
_ASKS_SECRET = re.compile(
    r"\b(send|share|give|tell|provide|type|enter|paste|reply with|what(?:'s| is))\b[^.?!\n]{0,40}\b(password|passcode|mfa code|verification code|one-time code|otp)\b", re.I)
MAX_EXCHANGES = 3  # employee messages without progress before the harness hands the problem to a human
_NEGATION = re.compile(r"\b(never|don'?t|do not|not|won'?t|no need)\b", re.I)


class LLMUnavailable(Exception):
    """The model could not be reached before any work was done; the caller falls back to playbooks."""


# ------------------------------------------------------------------ prompt context
def facts_block(ctx: AgentContext) -> str:
    emp = ctx.employee
    lines = [f"Today: {datetime.now().strftime('%A %Y-%m-%d %H:%M')}.",
             f"Employee: {emp['name']} ({emp['id']}). {pb._profile_line(emp['id'])}.",
             f"Devices: {pb._device_line(emp['id'])}."]
    open_issues = []
    for area, issue in ctx.state["issues"].items():
        if issue.get("resolved"):
            continue
        bad = failing(issue["findings"])
        parts = [f"{area}:"]
        if issue["findings"]:
            parts.append("failing now = " + ("; ".join(f"{c['check']} ({c['detail']})" for c in bad) if bad else "nothing (all checks passed at last run)"))
        if issue["actions"]:
            parts.append("fixes already tried = " + "; ".join(f"{a['action']} {'ok' if a['ok'] else 'REJECTED'}" for a in issue["actions"]))
        if issue.get("ticket_id"):
            parts.append(f"ticket #{issue['ticket_id']} already raised")
        open_issues.append(" ".join(parts))
    if open_issues:
        lines.append("Open issues in this conversation (state may be stale; re-run diagnostics to verify):\n  - " + "\n  - ".join(open_issues))
    awaiting = ctx.state.get("awaiting") or {}
    if awaiting.get("type") == "password_done":
        changed_at = get_state(emp["id"])["directory"].get("password_changed_at") or 0
        changed = changed_at >= awaiting.get("issued_at", 0)
        ctx.password_verified = changed
        lines.append("A password reset link was sent earlier. The directory shows the password "
                     + ("HAS been changed. Re-run diagnostics for the area the employee originally had trouble with to verify, "
                        "and tell them to sign in with the new password." if changed else
                        "has NOT been changed yet. If the employee says it is done, tell them it is not recorded and offer a fresh link."))
    if ctx.declined:
        lines.append("The employee asked you NOT to create a ticket. Do not offer one; they can say 'create a ticket' later.")
    try:
        open_tickets = [t for t in list_tickets(emp) if t["status"] != "resolved"][:5]
    except Exception:
        open_tickets = []
    if open_tickets:
        lines.append("Open tickets: " + "; ".join(f"#{t['id']} {t['title'][:60]} ({t['status']})" for t in open_tickets))
    return "\n".join(lines)


def history_messages(session_id: str, employee_id: str, limit: int = 6) -> tuple[list[dict], list[str]]:
    """Recent conversation for the model, plus every earlier employee message (for the ticket if one is needed)."""
    all_messages = [m for m in get_session(session_id, employee_id)["messages"] if m.get("role") in {"user", "assistant"}]
    employee_words = [str(m.get("content", ""))[:500] for m in all_messages if m["role"] == "user"]
    messages = all_messages[-limit:]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return [{"role": m["role"], "content": str(m.get("content", ""))[:600]} for m in messages], employee_words


# ------------------------------------------------------------------ model loop
def _inline_call(text: str):
    """Small models sometimes write a tool call as JSON text instead of using the tool-call channel."""
    import json
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"] in {t["name"] for t in tool_schemas()}:
        args = data.get("arguments") or data.get("parameters") or {}
        return ToolCall("inline_0", data["name"], args if isinstance(args, dict) else {})
    return None


def _label(name: str, args: dict, observation: dict) -> str:
    """Plain-language progress line for the chat page."""
    area = {"vpn": "VPN", "email": "email", "network": "network", "account": "account", "device": "device"}.get(args.get("area"), args.get("area", ""))
    if "error" in observation:
        return "Adjusting my approach"
    return {
        "run_diagnostics": f"Checked your {area}: " + (f"{len(observation.get('failed', []))} problem(s) found" if observation.get("failed") else "all checks pass"),
        "apply_fix": ("Fixed: " + str(observation.get("detail", ""))[:80]) if observation.get("applied") else "A fix was rejected, a technician is needed",
        "check_service_status": f"Service status: {args.get('service')} is {observation.get('status')}",
        "install_software": ("Installed " + str(observation.get("application"))) if observation.get("installed") else "Looked into your software request",
        "send_password_reset_link": "Prepared your private password reset link",
        "search_knowledge": "Read the IT knowledge base",
        "create_ticket": "Raised a ticket for the Tech Support team",
        "secure_account": "Secured your account",
        "mark_resolved": "Marked the issue resolved",
        "lookup": "Looked up your records",
    }.get(name, f"Ran {name}")


def _prefetch(ctx: AgentContext, messages: list[dict]) -> None:
    """Save a slow model step: when the words clearly point at one area, run its read-only checks first and show the
    model the result as if it had asked. Diagnostics change nothing, and the model can still check other areas."""
    if os.getenv("AGENT_PREFETCH", "1") != "1":
        return
    area = PREFETCH_AREA.get(keyword_intent(ctx.text))
    if area is None:
        return
    observation = execute(ctx, "run_diagnostics", {"area": area})
    ctx.tool_results[-1]["prefetched_by_harness"] = True
    progress.add(ctx.employee["id"], _label("run_diagnostics", {"area": area}, observation))
    call = ToolCall("prefetch_0", "run_diagnostics", {"area": area})
    messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments}]})
    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": observation_text(observation)})


def _speaks_about_instead_of_to(ctx: AgentContext, text: str) -> bool:
    """True if a reply talks ABOUT the employee ("the employee said...") instead of TO them."""
    first = ctx.employee["name"].split()[0]
    about = re.search(r"\bthe employee\b", text, re.I) or re.search(rf"\b{re.escape(first)}\b\s+(said|says|wants|is|has|reported|asked)\b", text, re.I)
    return bool(about) or (bool(text) and not re.search(r"\b(you|your|you're|you've)\b", text, re.I) and len(text) > 120)


def _rewrite_to_employee(ctx: AgentContext, llm, messages: list[dict], text: str, instruction: str | None = None) -> str:
    instruction = instruction or ("That reads as notes about the employee. Rewrite it as a short message written directly to the employee, "
                                  "using 'you'. No reasoning, no tool names. Output only the message.")
    messages = [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": instruction}]
    try:
        reply = llm.chat(messages, [])
        ctx.model_calls += 1
        return clean_text(reply.content)
    except LLMError:
        return ""


def run_model(ctx: AgentContext, llm, history: list[dict]) -> str:
    """Run the tool loop. Returns the model's final text (may be empty)."""
    employee_id = ctx.employee["id"]
    progress.add(employee_id, "Reading your message")
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + facts_block(ctx)}, *history,
                {"role": "user", "content": ctx.text}]
    _prefetch(ctx, messages)
    tools = tool_schemas()
    tool_count, final = 0, ""
    seen: dict[tuple, int] = {}
    for step in range(config.AGENT_MAX_STEPS):
        progress.add(employee_id, "Thinking" if step == 0 else "Thinking about the results")
        try:
            reply = llm.chat(messages, tools)
        except LLMError as error:
            if step == 0:
                raise LLMUnavailable(str(error)) from error
            ctx.model_error = str(error)
            return ""
        ctx.model_calls += 1
        ctx.reasoning_leaked = ctx.reasoning_leaked or reply.reasoning_leaked
        for key in ctx.usage:
            ctx.usage[key] += reply.usage.get(key, 0)
        calls = list(reply.tool_calls)
        if not calls and (inline := _inline_call(reply.content)):
            calls, reply.content = [inline], ""
        if not calls:
            final = reply.content
            break
        messages.append({"role": "assistant", "content": reply.content,
                         "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]})
        for call in calls:
            signature = (call.name, str(sorted(call.arguments.items())))
            seen[signature] = seen.get(signature, 0) + 1
            if tool_count >= config.AGENT_MAX_TOOL_CALLS:
                observation = {"error": "Tool budget for this message is used up. Write your final reply to the employee now."}
            elif seen[signature] > 2:
                observation = {"error": "You already made this exact call. Use the earlier result or take a different step."}
            else:
                observation = execute(ctx, call.name, call.arguments)
                tool_count += 1
                progress.add(employee_id, _label(call.name, call.arguments, observation))
            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": observation_text(observation)})
    else:
        ctx.budget_exhausted = True
    if not final.strip():  # budget spent or empty reply: one last attempt, no tools allowed
        progress.add(employee_id, "Writing the reply")
        messages.append({"role": "user", "content": "Now write your final reply to the employee in plain language, based on what the tools returned."})
        try:
            reply = llm.chat(messages, [])
            ctx.model_calls += 1
            final = reply.content
        except LLMError:
            final = ""
    final = clean_text(final)
    if final and _ACTION_CLAIM.search(final) and not ctx.actions_ok:
        # The reply says something was fixed, but no tool changed anything this turn. Never let that stand.
        progress.add(employee_id, "Checking my reply against what I actually did")
        ctx.record("harness_blocked_unperformed_claim", {"claim": final[:200]})
        final = _rewrite_to_employee(ctx, llm, messages, final,
                                     "Your reply says you fixed or changed something, but no tool made any change. Rewrite it honestly using only what the tool results "
                                     "show. If you have not changed anything, say so, say what you checked, and ask the employee ONE specific question (which app, "
                                     "the exact error text). Write directly to the employee using 'you'. Output only the message.")
        if final and _ACTION_CLAIM.search(final):
            final = ""
    if final and _speaks_about_instead_of_to(ctx, final):
        progress.add(employee_id, "Rewriting the reply")
        final = _rewrite_to_employee(ctx, llm, messages, final)  # empty if it fails: the evidence summary is used instead
        if final and _speaks_about_instead_of_to(ctx, final):
            final = ""
    return final


# ------------------------------------------------------------------ safety nets
def _scrub(text: str) -> str:
    text = re.sub(r"\S*/corporate-password\S*", "[link sent privately]", text)
    kept, removed = [], False
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        match = _ASKS_SECRET.search(sentence)
        if match and not _NEGATION.search(sentence[:match.end()]):
            removed = True
            continue
        kept.append(sentence)
    out = " ".join(kept).strip()
    if removed:
        out += "\n\nFor your security, never type your password or any verification code into this chat."
    return out


def _evidence_summary(ctx: AgentContext) -> str:
    lines = []
    for area in AREAS:
        issue = peek(ctx, area)
        if not issue or area not in ctx.diagnosed:
            continue
        bad = failing(issue["findings"])
        done = [a["detail"] for a in issue["actions"] if a["ok"]]
        if done:
            lines.append("What I fixed: " + "; ".join(done) + ".")
        lines.append(f"{pb.DOMAIN_LABEL[area]} check: " + (
            "still failing: " + "; ".join(f"{c['check']} ({c['detail']})" for c in bad) + "." if bad else "all checks pass."))
    return "\n".join(lines) or ("I haven't changed anything yet. So I can look in the right place, which app are you using, "
                                 "and what exactly do you see (the exact error text if there is one)?")


def _reason(area: str, check: dict, issue: dict) -> tuple[str, str]:
    label = pb.DOMAIN_LABEL[area]
    if check["category"] == "service_outage":
        return (f"{label}: active service incident",
                f"There is an active {label} service incident ({check['detail'].lower()}). It is not caused by the employee's device.")
    if check["owner"] == "agent":
        rejected = next((a["detail"] for a in issue["actions"] if a["action"] == check["fix"]), check["detail"])
        return (f"{label}: automatic fix rejected", f"Automatic fix for '{check['check']}' was rejected by the system: {rejected}")
    return (f"{label}: {check['detail']}"[:100], f"{check['check']}: {check['detail']}.")


def finalize(ctx: AgentContext, model_text: str) -> None:
    text = _scrub(model_text) or _evidence_summary(ctx)
    declined_notes = []

    # 1. Safety instruction for dangerous equipment, whatever the model did.
    if keyword_intent(ctx.text) == "safety":
        ctx.must_escalate.setdefault("hardware", ("Unsafe equipment reported", "Equipment reported as unsafe; it must be inspected and replaced."))
        text = ("Stop using the equipment now: unplug it only if that is safe, and move away. If there is smoke, fire or any immediate "
                "danger, follow your workplace emergency procedure and call emergency services. A support ticket is not an emergency response.\n\n" + text)

    # 2. Anything only a human can fix gets a ticket, even if the model forgot.
    def ticketed_now(area):
        issue = peek(ctx, area)
        return bool(issue and issue.get("ticket_id") in {t["id"] for t in ctx.created_tickets})

    targets = []
    for area in AREAS:
        issue = peek(ctx, area)
        if issue and area in ctx.diagnosed and human_needed(issue) and not ticketed_now(area):
            check = human_needed(issue)[0]
            targets.append((area, *_reason(area, check, issue), check["category"] if check["category"] not in {None, "other"} else None))
    for area, (title, reason) in ctx.must_escalate.items():
        if not ticketed_now(area):
            targets.append((area, title, reason, None))
    seen_areas = set()
    for area, title, reason, category in targets:
        if area in seen_areas or ticketed_now(area):
            continue
        seen_areas.add(area)
        ctx.record("harness_auto_escalation", {"area": area, "reason": reason})
        if pb.file_ticket(ctx, area, title, reason, category) is None:
            declined_notes.append(reason)

    # 2b. Stalled conversation: two rounds of questions and steps without a fix, a link or a ticket -> hand over.
    earlier = getattr(ctx, "earlier_employee_words", [])
    if (len(earlier) + 1 >= MAX_EXCHANGES and not ctx.created_tickets and not ctx.link_sent and not ctx.fix_applied and not ctx.resolved
            and not ctx.state.get("no_ticket") and followup_signal(ctx.text) != "positive" and keyword_intent(ctx.text) not in {"thanks", "greeting"}
            and not any(i.get("ticket_id") for i in ctx.state["issues"].values())):
        area = next((a for a in AREAS if peek(ctx, a) and a in ctx.diagnosed), None) or "other"
        issue = ctx.issue(area)
        for line in earlier[-4:]:
            if line not in issue["reports"]:
                issue["reports"].insert(0, line)
        ctx.record("harness_auto_escalation", {"area": area, "reason": "stalled conversation"})
        filed = pb.file_ticket(ctx, area, f"{pb.DOMAIN_LABEL.get(area, 'Support')}: unresolved after {len(earlier) + 1} messages",
                               f"The agent investigated and asked follow-up questions over {len(earlier) + 1} messages but could not identify or fix the cause. "
                               "The employee's own words are above; check the conversation for the agent's questions.")
        if filed is None:
            declined_notes.append("stalled")

    # 3. An expired password always gets the self-service link.
    awaiting = ctx.state.get("awaiting") or {}
    if not ctx.link_sent and awaiting.get("type") != "password_done":
        for area in AREAS:
            issue = peek(ctx, area)
            if issue and area in ctx.diagnosed and any(c["owner"] == "employee" for c in failing(issue["findings"])):
                ctx.record("harness_auto_password_link", {"area": area})
                ctx.blocks.append("Your corporate password has expired, which blocks sign-in. For security only you can choose the new one, so "
                                  "here is your personal reset link.\n\n" + pb.send_password_link(ctx, area))
                ctx.state["links_issued"] = ctx.state.get("links_issued", 0) + 1
                ctx.link_sent = True
                break

    # 4. Never leave an unverified or false "fixed" claim standing.
    still = [(a, c) for a in AREAS if (peek(ctx, a) and a in ctx.diagnosed) for c in failing(peek(ctx, a)["findings"])]
    if still and _CLAIM.search(text):
        text += "\n\nCorrection: my latest check still shows " + "; ".join(f"{c['check']}" for _, c in still) + ", so this is not fully fixed yet."

    # 5. Ticket summary, written by the harness so the numbers are always real.
    created = {item["result"]["ticket_id"]: item["result"]["created"] for item in ctx.tool_results
               if item["tool"] == "escalate_support_issue"}
    blocks = list(ctx.blocks)
    for ticket in ctx.created_tickets:
        blocks.append(f"Ticket #{ticket['id']} {'raised' if created.get(ticket['id'], True) else 'updated'} for the Tech Support team: {ticket['title']}\n"
                      "It already includes your description, your device, every check I ran and every fix I attempted, so a technician won't need to ask you to repeat anything.\n"
                      f"Initial complexity: {ticket['complexity']}. Provisional target: {ticket['resolution_estimate']} (a technician confirms timing).")
    if declined_notes:
        blocks.append("You asked me not to create a ticket, so I haven't. Say \"create a ticket\" whenever you want me to send everything above "
                      "to the Tech Support team.")
    ctx.answer = "\n\n".join([text, *blocks]).strip()

    # 6. Outcome and what the next turn should expect.
    nothing_left = not still
    if ctx.created_tickets:
        ctx.outcome = "escalated"
    elif declined_notes:
        ctx.outcome = "escalation_declined"
    elif ctx.link_sent:
        ctx.outcome = "awaiting_employee_action"
    elif ctx.resolved:
        ctx.outcome = "resolved"
    elif ctx.fix_applied and nothing_left:
        ctx.outcome = "fixed_pending_confirmation"
    elif text.rstrip().endswith("?") and not ctx.tool_results:
        ctx.outcome = "needs_clarification"
    else:
        ctx.outcome = "completed"
    if not ctx.link_sent and getattr(ctx, "password_verified", False):
        ctx.expect(None)  # the reset was confirmed; nothing is pending any more
    elif not ctx.link_sent and (ctx.state.get("awaiting") or {}).get("type") != "password_done":
        ctx.expect(None)
    if text.rstrip().endswith("?"):
        ctx.state["asked_question"] = True
