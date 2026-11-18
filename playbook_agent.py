"""Playbook agent (deterministic engine, v2). v3 uses it as the fallback when no model is reachable.

Flow for every message:
  1. Load the conversation's workflow state (what the agent is waiting for).
  2. If the employee is answering a pending question ("did it work?", "done"), continue that flow.
  3. Otherwise route the message (rules first, small model as fallback) to a playbook.
  4. The playbook diagnoses, applies allow-listed fixes, verifies, and escalates with full detail
     when it cannot resolve the issue.
  5. Save the turn (reset links redacted) and the new workflow state.
"""
import json
import re
import time

import playbooks as pb
from classifier import classify, declines_ticket, followup_signal, is_about_others, keyword_intent, wants_ticket
from database import connect
from sessions import create_session, get_session, save_turn
from tickets import pending_drafts

DOMAIN_FOR_INTENT = {"vpn": "vpn", "email": "email", "network": "network", "device_compliance": "device"}
CLARIFY = ("I want to help. Which app or device is affected (for example VPN, Outlook/email, Wi-Fi, "
           "a password, a software install or your laptop), and what error or behaviour do you see? "
           "If this isn't an IT problem, I may not be able to help with it.")


def load_state(session_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT state FROM session_state WHERE session_id=?", (session_id,)).fetchone()
    state = json.loads(row["state"]) if row else {}
    state.setdefault("awaiting", None)
    state.setdefault("issues", {})
    state.setdefault("no_ticket", False)
    return state


def save_state(session_id: str, state: dict):
    with connect() as db:
        db.execute("""INSERT INTO session_state VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET state=excluded.state""", (session_id, json.dumps(state)))


def route(ctx: pb.Context, text: str) -> None:
    intent, source = classify(text, ctx.state.get("last_reply", ""))
    ctx.record("route_message", {"intent": intent, "source": source})
    if intent in DOMAIN_FOR_INTENT:
        if intent == "device_compliance" and re.search(r"\b(slow|sluggish|lagg?(y|ing)|freez\w*|hang\w*|unresponsive)\b", text.lower()):
            ctx.issue("device")["probe_key"] = "slow"
        pb.fix_domain(ctx, DOMAIN_FOR_INTENT[intent])
    elif intent == "password":
        pb.password_playbook(ctx)
    elif intent == "software":
        pb.software_playbook(ctx, text)
    elif intent == "security":
        pb.security_playbook(ctx)
    elif intent == "safety":
        pb.safety_playbook(ctx)
    elif intent == "hardware":
        pb.hardware_playbook(ctx)
    elif intent == "office_app":
        pb.app_playbook(ctx)
    elif intent == "access":
        pb.access_playbook(ctx)
    elif intent == "service_status":
        pb.service_status_answer(ctx)
    elif intent == "ticket_status":
        pb.ticket_status_answer(ctx)
    elif intent == "list_devices":
        pb.devices_answer(ctx)
    elif intent == "how_to":
        pb.how_to_answer(ctx)
    elif intent == "sign_out":
        ctx.answer = "Signing you out now. Sign back in with your current password to continue."
        ctx.client_action, ctx.outcome, ctx.verbatim = "logout", "completed", True
    elif intent == "off_topic":
        off_topic_answer(ctx)
    elif intent == "scope":
        scope_answer(ctx, text)
    elif intent == "privilege":
        privilege_answer(ctx)
    elif intent == "create_ticket":
        explicit_ticket(ctx)
    elif intent == "greeting":
        ctx.answer = (f"Hi {ctx.employee['name']}! Tell me what isn't working (VPN, email, Wi-Fi, a password, "
                      "software you need, or your device) and I'll diagnose and fix it. If I can't, I'll raise a "
                      "ticket with all the details for the Tech Support team.")
    elif intent == "thanks":
        farewell = re.search(r"\b(bye|goodbye|good|fine|set|done|that'?s (all|it)|nothing|no thanks|take care|see )", text.lower())
        ctx.answer = ("Glad I could help. Reach out any time you need IT support." if farewell
                      else "You're welcome! Anything else I can help with?")
    else:
        clarify(ctx, text)


def off_topic_answer(ctx: pb.Context) -> None:
    ctx.answer = ("That's outside what I can help with. I'm the IT support assistant, so I can look into things like "
                  "VPN, email, Wi-Fi, passwords, software or your laptop. Is there something like that I can help you with?")
    ctx.outcome = "declined"
    ctx.verbatim = True


def scope_answer(ctx: pb.Context, text: str) -> None:
    """Decline requests to act on other people's accounts or to ignore the rules. Fixed text: no model rewording."""
    override = re.search(r"\b(ignore|disregard|forget|override|bypass)\b", text.lower())
    specific = override or is_about_others(text)
    ctx.answer = (("I can't skip my rules. " if override else "") +
                  ("I can only act on your own account, so I won't reset or change anyone else's password or access. "
                   "Changes for other people or for the whole company have to be done by IT security."
                   if specific else
                   "I can't do that. It's outside what I'm able to change or allowed to do on my own.") + "\n\n"
                  "If you need that, say \"create a ticket\" and I'll send the request to the Tech Support team. " +
                  ("If you want a reset for your own account, say \"reset my password\"." if specific else
                   "If you tell me what you're trying to get done, I may be able to help another way."))
    ctx.state["pending_escalation"] = {
        "domain": "other", "title": "Request to change other people's accounts or company-wide access" if specific
        else "Request the agent cannot perform",
        "reason": "The employee asked the agent to change other accounts or to bypass its rules. The agent declined "
                  "and made no changes. Needs a human decision.",
        "category": "other", "employee_reason": "You asked for changes to other accounts, which I can't do."}
    ctx.outcome = "declined"
    ctx.verbatim = True


def privilege_answer(ctx: pb.Context) -> None:
    """Admin rights are never granted by the agent. Fixed text, and a ready-to-send request if the employee wants one."""
    ctx.answer = ("I can't grant admin access. Administrator rights have to be approved by your manager and IT security, "
                  "and I only act within what IT has approved.\n\n"
                  "If you need it for a specific task, such as installing a program or changing a setting, tell me the task "
                  "and I may be able to do it another way. Otherwise say \"create a ticket\" and I'll send an access "
                  "request to the Tech Support team with the details.")
    ctx.state["pending_escalation"] = {
        "domain": "other", "title": "Request for administrator access",
        "reason": "The employee asked for administrator rights. The agent cannot grant them and made no changes. "
                  "Needs manager approval and an IT security decision; ask what task the access is for.",
        "category": "admin_install", "employee_reason": "Administrator access needs approval from your manager and IT security."}
    ctx.outcome = "declined"
    ctx.verbatim = True


def clarify(ctx: pb.Context, text: str) -> None:
    awaiting = ctx.state.get("awaiting") or {}
    if awaiting.get("type") == "clarify":
        # Second unclear message: hand it to a human with the full conversation rather than looping.
        ctx.issue("other")
        pb.escalate(ctx, "other", "Support request needs technician review",
                    "The agent could not identify an affected system it can diagnose.",
                    lead="I couldn't match this to a system I can check automatically.")
        return
    ctx.answer = CLARIFY
    ctx.outcome = "needs_clarification"
    ctx.expect("clarify", None, text=text)


def explicit_ticket(ctx: pb.Context) -> None:
    ctx.state["no_ticket"] = ctx.declined = False
    pending = ctx.state.get("pending_escalation")
    if pending:
        pb.escalate(ctx, pending["domain"], pending["title"], pending["reason"], pending["category"],
                    employee_reason=pending.get("employee_reason"))
        return
    open_issues = [d for d, i in ctx.state["issues"].items() if not i["resolved"]]
    if not open_issues and ctx.state["issues"]:
        latest = list(ctx.state["issues"])[-1]  # the employee wants a person on what we just discussed
        ctx.state["issues"][latest]["resolved"] = False
        open_issues = [latest]
    if open_issues:
        domain = open_issues[-1]
        pb.escalate(ctx, domain, f"{pb.DOMAIN_LABEL.get(domain, domain)}: employee requested a technician",
                    "The employee asked for a technician.", employee_reason="You asked for a technician.")
        return
    ctx.answer = "Sure. What's the problem? Describe what isn't working and I'll check it first; if I can't fix it, I'll raise the ticket."
    ctx.outcome = "needs_clarification"
    ctx.expect("clarify", None, text="")


def continue_flow(ctx: pb.Context, awaiting: dict, text: str) -> bool:
    """Handle a reply to the agent's last question. Return False to route as a fresh message."""
    kind, domain = awaiting.get("type"), awaiting.get("domain")
    signal = followup_signal(text)
    if kind == "clarify" and pb.says_fixed(text, signal):
        ctx.answer = "Great, glad it's working again! No ticket was needed. Anything else I can help with?"
        ctx.outcome = "resolved"
        ctx.expect(None)
        return True
    if kind == "clarify":
        combined = (awaiting.get("text", "") + " " + text).strip()
        if domain == "software":
            pb.software_playbook(ctx, combined)
            return True
        intent, _ = classify(combined, ctx.state.get("last_reply", ""))
        if intent == "unknown":
            clarify(ctx, combined)
        else:
            ctx.text = combined
            route(ctx, combined)
        return True
    if kind == "probe":
        if pb.says_fixed(text, signal):
            ctx.issue(domain)["resolved"] = True
            ctx.answer = "Great, glad it's working again! No ticket was needed. Anything else I can help with?"
            ctx.outcome = "resolved"
            ctx.expect(None)
            return True
        intent, _ = classify(text, ctx.state.get("last_reply", ""))
        if intent in {"software", "security", "safety", "scope", "privilege", "off_topic"}:
            return False  # the answer names an app the agent can repair, or a security/safety matter: handle that
        pb.fix_domain(ctx, domain)  # records the answer, re-checks, fixes anything new, otherwise hands over
        return True
    if kind == "probe_app":
        pb.app_followup(ctx, text, signal)
        return True
    if kind == "password_done":
        if signal == "negative" or "link" in text.lower() and signal != "positive":
            pb.password_problem(ctx, awaiting)
        elif signal == "positive":
            pb.password_done(ctx, awaiting)
        else:
            return False
        return True
    if kind == "escalated":
        if signal == "positive":
            ctx.answer = (f"You're welcome! Ticket #{awaiting['ticket_id']} is with the Tech Support team. "
                          "You can follow it under My tickets. Anything else?")
            ctx.expect(None)
        elif signal == "negative":
            if domain in DOMAIN_FOR_INTENT.values():
                pb.fix_domain(ctx, domain)  # fresh diagnostics are appended to the same ticket
            else:
                pb.escalate(ctx, domain, awaiting["title"], "Employee added new information (see conversation).",
                            awaiting.get("category"))
        else:
            return False
        return True
    if kind == "confirm_fix":
        if signal == "positive":
            ctx.issue(domain)["resolved"] = True
            ctx.answer = "Great, glad it's working again! No ticket was needed. Anything else I can help with?"
            ctx.outcome = "resolved"
            ctx.expect(None)
        elif signal == "negative":
            if domain in {"software"}:
                pb.escalate(ctx, domain, "Software still not working after automated install",
                            "The application still fails after the automated install/repair.", category="admin_install")
            else:
                pb.fix_domain(ctx, domain)  # re-diagnose; escalates if nothing new can be fixed
        else:
            return False
        return True
    return False


def run_playbook_agent(question: str, employee: dict, session_id: str | None = None) -> dict:
    import progress
    started = time.monotonic()
    progress.add(employee["id"], "Reading your message")
    had_session = bool(session_id)
    if session_id:
        get_session(session_id, employee["id"])  # ownership check (404 otherwise)
    else:
        session_id = create_session(employee["id"])
    state = load_state(session_id)
    if declines_ticket(question):
        state["no_ticket"] = True
    elif wants_ticket(question):
        state["no_ticket"] = False
        state["human_requested"] = True
    ctx = pb.Context(employee, session_id, question, state, declined=state["no_ticket"])
    ctx.first_turn = not had_session

    try:
        awaiting = state.get("awaiting")
        if wants_ticket(question) and (state.get("pending_escalation") or awaiting):
            explicit_ticket(ctx)
        elif not (awaiting and continue_flow(ctx, awaiting, question)):
            if awaiting and awaiting.get("type") != "clarify" and keyword_intent(question) != "off_topic":
                ctx.expect(None)  # (a side question keeps the open question alive)
            route(ctx, question)
    except Exception:
        ctx.answer = ("Something went wrong while I was working on this. Nothing further is running; your "
                      "conversation is saved. Please try again, or contact IT support directly if it repeats.")
        ctx.outcome = "tool_error"
        import logging
        logging.getLogger("support").exception('{"event": "agent_error"}')

    import reply_writer
    if ctx.first_turn and ctx.answer and not ctx.verbatim and not re.match(r"\s*(hi|hello|hey)\b", ctx.answer, re.I):
        ctx.answer = f"Hi {(employee.get('name') or '').split(' ')[0] or 'there'},\n\n" + ctx.answer
    reply_meta = reply_writer.polish(ctx)
    stored = ctx.answer
    for link in ctx.secret_links:
        stored = stored.replace(link, "[one-time reset link - shown once, not stored]")
    turn = [{"role": "user", "content": question}]
    turn += [{"role": "tool", "tool_name": item["tool"], "content": json.dumps(item["result"])} for item in ctx.tool_results]
    turn.append({"role": "assistant", "content": stored})
    save_turn(session_id, turn)
    ctx.state["last_reply"] = stored[:400]
    save_state(session_id, ctx.state)
    return {
        "session_id": session_id,
        "answer": ctx.answer,
        "outcome": ctx.outcome,
        "routing": "playbook",
        "simulated": True,
        "sources": sorted(ctx.sources),
        "tool_results": ctx.tool_results,
        "created_tickets": ctx.created_tickets,
        "pending_tickets": pending_drafts(employee["id"], session_id),
        "reset_links": ctx.secret_links,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "client_action": getattr(ctx, "client_action", None),
        "reply_source": reply_meta["source"],
        "reply_model": reply_meta.get("model"),
        "reply_ms": reply_meta.get("ms"),
        "reply_fallback": reply_meta.get("fallback"),
        "understanding": next((t["result"].get("source") for t in ctx.tool_results if t["tool"] == "route_message"), "follow-up"),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
