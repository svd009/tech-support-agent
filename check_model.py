"""Is your model good enough to drive the agent? Run this before chatting.

    python check_model.py

It asks the configured model (LLM_PROVIDER / AGENT_MODEL) to handle three tiny support requests that
each require a specific tool call, and reports whether it called the right tool with valid arguments,
and how long each call took. A model that fails these will not drive the full agent reliably.
"""
import json
import time

from agent_tools import tool_schemas, validate
from llm import LLMError, get_llm

SYSTEM = "You are an IT support agent. Use the tools to investigate. Call a tool; do not just describe what you would do."
CASES = [
    ("My VPN says authentication failed.", {"run_diagnostics", "send_password_reset_link", "check_service_status"}),
    ("I need Slack installed on my laptop.", {"install_software"}),
    ("Is the email service down for everyone right now?", {"check_service_status"}),
]


def main():
    try:
        model = get_llm()
    except LLMError as error:
        raise SystemExit(str(error))
    print(f"Model: {getattr(model, 'model', model.name)} via {model.name}\n")
    good, leaks, total_seconds = 0, 0, 0.0
    for text, acceptable in CASES:
        started = time.monotonic()
        try:
            reply = model.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}], tool_schemas())
        except LLMError as error:
            raise SystemExit(f"FAILED: {error}")
        seconds = time.monotonic() - started
        total_seconds += seconds
        leaks += int(reply.reasoning_leaked)
        if not reply.tool_calls:
            verdict = f"NO TOOL CALL (it said: {reply.content[:80]!r})"
        else:
            call = reply.tool_calls[0]
            _, problem = validate(call.name, call.arguments)
            if problem:
                verdict = f"INVALID CALL {call.name} {json.dumps(call.arguments)}: {problem}"
            elif call.name in acceptable:
                verdict, good = f"OK  {call.name} {json.dumps(call.arguments)}", good + 1
            else:
                verdict = f"WRONG TOOL {call.name} {json.dumps(call.arguments)}"
        print(f"[{seconds:5.1f}s, {reply.usage['output_tokens']} output tokens{', REASONED FIRST' if reply.reasoning_leaked else ''}] {text}\n         -> {verdict}")
    print(f"\n{good}/{len(CASES)} correct, {total_seconds / len(CASES):.0f}s per call on average.")
    if good < len(CASES):
        print("This model is unreliable at tool calling. Try another AGENT_MODEL, or LLM_PROVIDER=anthropic.")
    if leaks:
        print("This model writes out its reasoning before answering. That is slow (every thinking token costs CPU time). "
              "Prefer a non-thinking model, for example: ollama pull llama3.2:3b  (then set AGENT_MODEL=llama3.2:3b).")
    elif total_seconds / len(CASES) > 45:
        print("Calls are slow for chat. Try a smaller non-thinking model, or LLM_PROVIDER=anthropic for a live demo.")
    if good == len(CASES) and not leaks:
        print("Good enough to try the full agent.")


if __name__ == "__main__":
    main()
