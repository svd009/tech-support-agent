"""Support agent entry point (v4: harness-driven pipeline, model only for understanding).

run_agent() is what the web app calls for every employee message.

  AGENT_MODE=pipeline  (default) code runs the investigation: understand -> check -> fix -> verify -> reply/ticket.
                       Keyword rules understand most messages instantly; the model is asked for one short JSON
                       label only when the rules cannot tell what is affected. Replies are built from the action log.
  The three modes below are the older v3 experiment (the model drives a tool loop). Slow on CPU; kept for comparison.

  AGENT_MODE=auto      (default) the model drives the tool loop (agent_loop.py). If the model cannot be
                       reached, the deterministic playbook engine (playbook_agent.py) answers instead.
  AGENT_MODE=model     model only; an unreachable model produces an honest "agent offline" reply.
  AGENT_MODE=playbook  deterministic playbooks only (v2 behaviour, no model needed).
"""
import json
import logging
import os
import time

import playbook_agent
import progress
from agent_loop import LLMUnavailable, history_messages, run_model, finalize
from agent_tools import AgentContext
from classifier import declines_ticket, wants_ticket
from llm import get_llm, LLMError
from playbook_agent import load_state, save_state
from sessions import create_session, get_session, save_turn
from tickets import pending_drafts

logger = logging.getLogger("support")
OFFLINE = ("My reasoning model is offline right now, so I can't investigate this myself. "
           "Please try again in a few minutes, or contact IT support directly if it's urgent.")


def run_agent(question: str, employee: dict, session_id: str | None = None) -> dict:
    mode = os.getenv("AGENT_MODE", "pipeline").lower()
    if mode in {"pipeline", "playbook"}:
        progress.start(employee["id"])
        try:
            result = playbook_agent.run_playbook_agent(question, employee, session_id)
        finally:
            progress.finish(employee["id"])
        result["routing"] = mode
        return result
    started = time.monotonic()
    if session_id:
        get_session(session_id, employee["id"])  # ownership check (404 otherwise)
        new_session = False
    else:
        session_id, new_session = create_session(employee["id"]), True
    progress.start(employee["id"])
    try:
        llm = get_llm()
        return _model_turn(question, employee, session_id, llm, started)
    except (LLMUnavailable, LLMError) as error:
        logger.warning('{"event": "model_unavailable", "reason": %s}', json.dumps(str(error)))
        if mode == "model":
            return _offline(question, employee, session_id, started, str(error))
        progress.add(employee["id"], "The AI model is unreachable, using the built-in playbooks")
        result = playbook_agent.run_playbook_agent(question, employee, session_id)
        result["routing"] = "playbook-fallback"
        result["fallback_reason"] = str(error)
        return result
    finally:
        progress.finish(employee["id"])


def _model_turn(question, employee, session_id, llm, started) -> dict:
    state = load_state(session_id)
    if declines_ticket(question):
        state["no_ticket"] = True
    elif wants_ticket(question):
        state["no_ticket"] = False
    ctx = AgentContext(employee, session_id, question, state, declined=state["no_ticket"])
    history, earlier_words = history_messages(session_id, employee["id"])
    ctx.earlier_employee_words = earlier_words
    explicit_ticket_request = wants_ticket(question) and state.get("pending_escalation") and not state["no_ticket"]
    try:
        if explicit_ticket_request:
            # The employee changed their mind: send the parked escalation exactly as prepared.
            import playbooks as pb
            pending = state["pending_escalation"]
            pb.file_ticket(ctx, pending["domain"], pending["title"], pending["reason"], pending["category"])
            ctx.blocks.append("Done: I've sent everything to the Tech Support team.")
            text = ""
        else:
            text = run_model(ctx, llm, history)
        finalize(ctx, text)
    except LLMUnavailable:
        raise
    except Exception:
        logger.exception('{"event": "agent_error"}')
        ctx.answer = ("Something went wrong while I was working on this. Nothing further is running; your conversation is "
                      "saved. Please try again, or contact IT support directly if it repeats.")
        ctx.outcome = "tool_error"
    return _finish(ctx, question, started, "model")


def _offline(question, employee, session_id, started, reason) -> dict:
    state = load_state(session_id)
    ctx = AgentContext(employee, session_id, question, state, declined=state["no_ticket"])
    ctx.answer, ctx.outcome = OFFLINE, "tool_error"
    result = _finish(ctx, question, started, "offline")
    result["fallback_reason"] = reason
    return result


def _finish(ctx, question, started, routing) -> dict:
    stored = ctx.answer
    for link in ctx.secret_links:
        stored = stored.replace(link, "[one-time reset link - shown once, not stored]")
    turn = [{"role": "user", "content": question}]
    turn += [{"role": "tool", "tool_name": item["tool"], "content": json.dumps(item["result"])} for item in ctx.tool_results]
    turn.append({"role": "assistant", "content": stored})
    save_turn(ctx.session_id, turn)
    save_state(ctx.session_id, ctx.state)
    return {
        "session_id": ctx.session_id,
        "answer": ctx.answer,
        "outcome": ctx.outcome,
        "routing": routing,
        "simulated": True,
        "sources": sorted(ctx.sources),
        "tool_results": ctx.tool_results,
        "created_tickets": ctx.created_tickets,
        "pending_tickets": pending_drafts(ctx.employee["id"], ctx.session_id),
        "reset_links": ctx.secret_links,
        "usage": dict(getattr(ctx, "usage", {"input_tokens": 0, "output_tokens": 0})),
        "model_calls": getattr(ctx, "model_calls", 0),
        "reasoning_leaked": getattr(ctx, "reasoning_leaked", False),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
