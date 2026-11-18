# Tech Support Agent v5

v5 adds a **reply writer**: a hosted model (Amazon Bedrock, Anthropic API, or local Ollama) rewrites the
answer in natural wording using only verified facts from the action log, the built-in draft, and the top
passage from `knowledge/` (RAG, retrieval-augmented generation). The code still investigates and fixes.
The harness rejects any model reply that claims an action that did not run, adds or drops ticket numbers
or links, invents numbers, asks for a password or code, drops the question, or leaks reasoning. Any
rejection, timeout or error silently falls back to the built-in reply.

## Turn on the model (PowerShell)

    pip install boto3
    aws configure
    $env:REPLY_PROVIDER = "bedrock"
    $env:REPLY_MODEL = "<model id from the Bedrock console>"
    $env:AWS_REGION = "us-east-1"
    python check_reply_model.py
    python check_reply_model.py --list

`check_reply_model.py` sends 3 sample prompts and prints seconds, whether the harness would accept the
reply, and the reply text. Start the server as before; the chat footer shows "model reply" or
"built-in reply used: <reason>". Check Cost Explorer after the first calls to confirm AWS credits cover
the chosen model.

# Tech Support Agent v4 (base)

**What changed in v4:** the harness (plain Python) now runs the whole investigation, and the model has one small
job. Measured on the real app, the v3 model loop took 320 to 386 seconds per message and added nothing to the
answer; the v4 pipeline answers the same messages in well under a second.

| Step | Who does it | Detail |
| --- | --- | --- |
| 1 Understand | Keyword rules, then the model only if the rules cannot decide | One JSON label, 45 s limit, otherwise one clarifying question |
| 2 Investigate | Code | Runs every read-only check for that area |
| 3 Fix and verify | Code | Applies allow-listed fixes for failed checks, then re-checks |
| 4 Reply or ticket | Code | Reply is built from the action log, so it can only state what actually ran |

When every check passes, the agent says it changed nothing, suggests one safe step, asks one targeted question,
and only then raises a ticket that includes the answer. Set `AGENT_MODE=pipeline` (the default). If your shell
still has `AGENT_MODE=model` or `auto` from v3, change it or remove it.

Blind evaluation (`python blind_eval.py --rounds 20 --mode playbook`): 186 of 187 problems handled correctly.

---

The sections below describe the older v3 model loop, which is still available with `AGENT_MODE=auto` or `model`.

# Tech Support Agent: version 3 (model + harness)

An employee-facing IT support agent built with FastAPI, SQLite and a language model. The model **reads
the problem in the employee's own words, investigates with tools, fixes what it is allowed to fix, verifies
the fix, and asks questions when it needs more**. The harness (plain Python) owns everything that must be
right: identity, permissions, verification, tickets, password links and safety. When a human really is
needed, the ticket already holds the employee's words, device, every check and every fix attempted.
All company systems are simulated; see "Making it real".

## How it works

```
employee message
   |
   v
HARNESS builds the prompt: rules + who the employee is + open issues, tickets, password-reset state
   |
   v
MODEL loop (up to 8 steps)  <------------------------------------------+
   | picks a tool                                                       |
   v                                                                    |
HARNESS validates the call, injects the employee identity, runs it,     |
verifies fixes automatically, returns a compact result  ----------------+
   |
   v  model writes the reply
HARNESS safety nets: ticket anything only a human can fix, send the reset link for an expired
password, correct false "fixed" claims, strip passwords/links from model text, add safety text
```

**What the model decides:** which area to investigate (a "VPN problem" may be the network, a password or a
certificate), which checks and fixes to run, how to read the results, when to ask a question, how to word the reply.

**What the harness guarantees, whatever the model does** (each one has a test that tries to cheat it):

| Rule | Enforced by |
|---|---|
| The employee identity comes from the login, never from tool arguments | `agent_tools.validate` |
| A fix only runs if a failed check of this employee lists it | `apply_fix` |
| Every fix is re-checked automatically; "fixed" needs passing checks | `apply_fix`, `finalize` |
| No ticket without diagnostics, and none while an automatic fix is untried | `create_ticket` |
| If checks all pass, the model must search the knowledge base or ask first (once) | `create_ticket` gate |
| A fault only a human can fix always gets a ticket, even if the model forgets | `finalize` |
| Two rounds of questions without progress hand over to a human | `finalize` (stalled conversation) |
| An expired password always gets the one-time link; the model never sees or repeats it | `send_password_reset_link`, `finalize` |
| Model text can't ask for a password or MFA code | `_scrub` in `agent_loop.py` |
| Resolved only when the employee confirms and the checks pass | `mark_resolved` |
| Smoke, fire or a swollen battery always gets the safety text and a hardware ticket | `finalize` |
| A tool or model failure never crashes the turn; an unreachable model falls back to the v2 playbooks | `agent.py` |

## Choose the model

The loop needs a model that can call tools. The 0.6B model used by the old router **cannot do this reliably**.

| Option | Setup | Notes |
|---|---|---|
| Local, free | `ollama pull qwen3:4b` (default `AGENT_MODEL`) | Runs on an 8 GB CPU machine, but expect tens of seconds per step. A conversation can take a minute or two. |
| Claude API | `$env:LLM_PROVIDER = "anthropic"; $env:ANTHROPIC_API_KEY = "..."` | Fast and the most reliable at tool use. Default model `claude-haiku-5-5` (`AGENT_MODEL` to change). Message text and tool results are sent to the API. |

**Speed on a CPU.** Prefill dominates: every model step re-reads the instructions and tool list. v3 keeps the prompt small (about 900
tokens), has the harness run the obvious read-only diagnostic before the first model step, keeps the model loaded
(`OLLAMA_KEEP_ALIVE`, default 30m) and shows live progress in the chat. Thinking models (the Ollama `qwen3:4b` tag may reason before every answer even
with thinking turned off) are much slower because every reasoning token costs CPU time; the harness strips that text so it never reaches the
employee, but you still pay for it. If `check_model.py` prints "REASONED FIRST", try a non-thinking model such as `llama3.2:3b`.
Optional Ollama server settings that reduce memory and time: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0` (set before starting Ollama).

Before chatting, run `python check_model.py`. It tests tool calling with three tiny requests and tells you whether
the model is good enough. `GET /agent/status` shows which engine is active.

`AGENT_MODE`: `auto` (default; model, with the v2 playbooks as backup if the model is unreachable), `model` (never fall back),
`playbook` (v2 only, no model needed).

## Quick start (Windows PowerShell)

```powershell
Set-Location D:\VOZON\tech-support-agent
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen3:4b
.\.venv\Scripts\python.exe database.py
.\.venv\Scripts\python.exe demo_company.py
.\.venv\Scripts\python.exe check_model.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The answer card shows which engine replied ("AI agent" or "playbook engine"), the model steps, and every tool call with its
arguments in the "Behind the answer" panel.

## Demo company and random test rounds

```powershell
.\.venv\Scripts\python.exe demo_company.py          # Corvane Payments: 10 employees in 2 teams + itdesk, all healthy
.\.venv\Scripts\python.exe incidents.py roll        # random round: who breaks, and why, is unknown to you
.\.venv\Scripts\python.exe incidents.py symptoms    # what each person is experiencing (never the cause)
.\.venv\Scripts\python.exe incidents.py score       # after chatting: reveal causes and grade the agent
.\.venv\Scripts\python.exe blind_eval.py --rounds 20 --mode playbook   # automated blind evaluation, v2 engine
.\.venv\Scripts\python.exe blind_eval.py --rounds 12 --mode stub       # v3 harness with an offline stand-in model (plumbing check only)
.\.venv\Scripts\python.exe blind_eval.py --rounds 2 --people 4 --mode real   # v3 with YOUR model (slow on CPU; --people limits cost)
```

In the web app, a signed-in employee sees a dashed "Demo round" box listing their current symptoms.
There are no pre-defined problems. Each round about 40% of employees have nothing wrong, 40% have one
problem and 20% have two or three. The odds depend on the person: remote staff get more VPN trouble,
a new hire gets missing licences, and only office staff use printers. Several causes share one symptom,
and about a third of the catalogue is hidden from the agent's diagnostics (correct answer: a ticket).
Ground truth is kept in the `incidents` table, which the agent code never reads; a test enforces this.

## Targeted testing: inject a specific fault

For checking one behaviour on purpose (not blind), inject faults with `simulate.py` (`--list` shows them all).

| Command | Then say | Expected |
|---|---|---|
| `python simulate.py alice vpn_stale_session vpn_cert_expired` | My vpn is not working, fix it. | Fixes both, verifies, asks you to retry |
| `python simulate.py alice vpn_password_expired` | VPN says authentication failed | Reset link + steps; "done" -> verified |
| `python simulate.py alice account_locked` | I forgot my password | Unlocks, sends link |
| `python simulate.py alice vpn_no_license` | VPN won't connect | Ticket (licence needs an admin) |
| `python simulate.py alice block:renew_vpn_certificate vpn_cert_expired` | VPN certificate error | Fix rejected -> ticket |
| `python simulate.py --service email degraded` | Outlook isn't receiving emails | Outage ticket, not your device |
| `python simulate.py alice mailbox_full` | Can't send email | Archives old mail |
| `python simulate.py alice wifi_adapter_disabled dns_stale` | My wifi is not working | Re-enables adapter, flushes DNS |
| (none) | Please install Zoom / I need Photoshop | Installs / approval ticket |
| (none) | I keep getting MFA prompts I didn't request | Guidance, sessions revoked, security ticket, reset link |
| `python simulate.py alice --reset` | | Back to healthy |

Log in as `support` to see the tickets in the Ticket queue.

## API notes

`POST /chat` returns `answer`, `outcome`, `tool_results` (every diagnostic, fix and verification),
`created_tickets`, `reset_links` (one-time links for this response only) and `session_id`.
Outcomes: `fixed_pending_confirmation`, `resolved`, `awaiting_employee_action`, `escalated`,
`escalation_declined`, `needs_clarification`, `completed` (information answers), `tool_error`.

Two separate password systems: `/corporate-password` (issued by the agent) changes the simulated
corporate password used for VPN, email and Windows. The existing `/reset-password` page still handles the
**Support Desk login** itself (forgot/change, technician-issued recovery), for people who can't sign in to chat.

## Files

| File | Purpose |
|---|---|
| agent.py | Entry point: runs the model loop, falls back to playbooks if the model is unreachable |
| agent_loop.py | The loop: prompt, session facts, step budget, safety nets applied after the model finishes |
| agent_tools.py | The 10 tools the model can call, with the rules enforced on every call |
| llm.py | Model providers (Ollama, Claude API) behind one interface |
| rule_model.py | Offline stand-in "model" for testing the harness (not intelligent) |
| check_model.py | Quick tool-calling test for your configured model |
| playbook_agent.py | v2 deterministic engine (the fallback), unchanged behaviour |
| playbooks.py | Diagnose -> fix -> verify -> escalate logic for the fallback; shared ticket builder |
| it_systems.py | Simulated IT systems: per-employee state, diagnostics, allow-listed remediation actions |
| classifier.py | Keyword routing, follow-up detection ("yes" / "still not working"), model fallback |
| model_client.py | Small-model router used only by the v2 playbook engine |
| simulate.py | Fault injection CLI for demos |
| auth.py | Login/tokens; Support Desk recovery; corporate password reset links |
| tickets.py | Escalation (dedup + update), drafts, support updates |
| static/corporate-password.* | One-time corporate password reset page |
| knowledge/*.md | Employee-facing how-to articles (used for "how do I..." questions) |
| tests/test_agent_flows.py | v2 playbook flows, end to end, with simulated faults |
| tests/test_agent_loop.py | v3 loop and guardrails, using scripted and deliberately misbehaving models |

## Making it real

Each function in `it_systems.py` is the boundary to replace with a real API call. Diagnostics and fixes
for the directory map to Entra ID or Okta, VPN to the gateway plus Intune/MDM, mail to Exchange Online or Google Workspace,
network and device to RMM or Intune remediation scripts, software to Intune or the company portal, and tickets to
ServiceNow or Jira Service Management. Keep the same shape (`diagnose()` returns checks, `run_action()` runs one
allow-listed action) and the playbooks don't change.

## Docker (optional; native setup first)

Docker packages the Python API; it does not package or download Ollama models.

```powershell
docker compose up --build -d
docker compose exec api python seed.py alice --name "Alice Employee"
docker compose exec api python seed.py support --name "IT Support" --role it_support
```

Stop native Uvicorn first to free port 8000. Compose binds the API only to host loopback.
The named volume is a SEPARATE database from your Windows `data/support.db`; provision accounts
inside it. `docker compose down` preserves it; `down -v` would delete it.

The container contacts host Ollama through host.docker.internal. If this cannot reach Ollama's
loopback-only listener, use the native setup while configuring Docker-to-host networking. Do not
open unauthenticated Ollama to a public network. Docker build/runtime was not verified here.

## Limits

* Systems, statuses and devices are simulated. A "fix" only changes the simulated state.
* **Not yet verified with a real model.** Everything here was tested with scripted and deliberately misbehaving
  stand-ins, which proves the harness. How well your chosen model understands free-form wording, picks tools and
  writes replies must be measured on your machine: run `check_model.py`, then `blind_eval.py --mode real`.
* Small local models (4B and below) are the weak point: they may skip tools, send malformed calls or write vague replies.
  The harness limits the damage (retries, refusals with hints, safety nets, fallback), but it can't make a weak model smart.
* Causes the simulated checks can't see (add-ins, shared drives, file corruption, printers) are handled by asking questions,
  searching the knowledge base and, if that doesn't resolve it, a ticket after two rounds without progress.
* SQLite stores conversations in plaintext (passwords and tokens are hashed; reset links are redacted).
* With `LLM_PROVIDER=anthropic`, employee messages and tool results are sent to the API.
* Single process; one chat at a time (HTTP 409 otherwise).

## Official references

- https://docs.ollama.com/api/chat
- https://fastapi.tiangolo.com/tutorial/security/
- https://docs.python.org/3/library/sqlite3.html
- https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
