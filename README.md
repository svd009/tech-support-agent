# Tech Support Agent

An AI help desk for employees. You describe an IT problem in plain language, and the agent investigates it, fixes what it is allowed to fix, and tells you what it did. If it cannot fix the problem, it raises a complete ticket for a human technician, so nobody has to repeat themselves.

It runs against a simulated company, so it is safe to try and easy to test.

## What it can do

- Understand free-form messages: VPN, email and Microsoft 365, Wi-Fi and network, laptop health, software installs, and account or password problems.
- Run read-only checks, apply a small set of approved fixes, then re-check to confirm the fix worked.
- Let an employee change their password through a one-time link. The agent never asks for a password in chat.
- Raise tickets that include the problem, every check that was run, and the employee's answers.
- Post technician status updates (in progress, resolved) back into the employee's chat.
- Politely decline things it must not do: admin rights, other people's accounts, off-topic questions.

## How it works

The agent is a **model plus a harness**. The two have different jobs:

| Part | Job |
|---|---|
| Harness (plain Python) | Decides what to check, what to fix, and when to hand over to a human. Every action is logged. |
| Model (optional) | Rewrites the answer in natural wording, using only the facts the harness verified. |

```
message -> understand -> investigate -> fix -> verify -> reply or ticket
```

The reply is built from the action log, so the agent cannot claim a fix that did not happen. When a model writes the reply, the harness checks it before it is shown. A reply is rejected if it invents an action, changes a ticket number, asks for a password, or leaves out a question. A rejected or slow reply falls back to the built-in wording, so the agent works with no model at all.

Company knowledge (short guides on VPN, email, passwords and so on) is retrieved at question time and given to the model as context.

## Quick start

You need Python 3.13 or newer.

```
python -m venv .venv
```

Activate the virtual environment, then:

```
pip install -r requirements.txt
python database.py
python demo_company.py
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open the address printed in the terminal. `demo_company.py` creates ten demo employees in two teams plus an `itdesk` account for the technician view, and asks once for a shared demo password. Run `python demo_company.py --list` to see the people.

The agent works immediately with its built-in replies. A model is optional.

## Optional: use a hosted model for replies

Set these in the same terminal session before starting the server:

```
REPLY_PROVIDER=bedrock      (none, bedrock, anthropic or ollama)
REPLY_MODEL=<model id from your provider>
AWS_REGION=<your region>
```

Credentials come from your normal AWS configuration or environment variables. They are never stored in the project. To test the connection before starting the app:

```
python check_reply_model.py
```

## Try it with real problems

The simulator can break things on purpose, so the agent has something to find and fix.

```
python simulate.py ben.carter mailbox_full mail_sync_stuck
python simulate.py --list
python simulate.py ben.carter --reset
```

Sign in as that employee and report the problem in your own words. The "Behind the answer" panel shows every check and fix.

For a blind test, `python incidents.py roll` breaks several employees at random, `python incidents.py symptoms` shows what they would say, and `python incidents.py score` reveals the hidden causes and grades the agent.

## Tests

```
python -m unittest discover -s tests
```

There are over 170 automated tests, plus a randomized evaluation script (`blind_eval.py`) that injects faults and checks whether the agent finds the right cause.

## Project layout

| Module | Purpose |
|---|---|
| `main.py` | Web API and chat endpoint |
| `playbook_agent.py`, `playbooks.py` | The harness: routing, investigation, fixes, escalation |
| `classifier.py` | Understands what a message is about |
| `reply_writer.py` | Model-written replies and the safety checks on them |
| `retrieval.py`, `knowledge/` | Company knowledge search |
| `it_systems.py` | The simulated VPN, email, network and device systems |
| `tickets.py` | Tickets and status updates |
| `auth.py` | Sign-in and one-time password reset links |
| `static/` | The web interface |

## Limits

- All systems are simulated. There are no real VPN, mailbox or directory connections.
- Keyword rules do the first pass at understanding messages, so unusual phrasing can still be missed.
- Moving to a real company means replacing the simulated systems with real integrations and adding approvals and audit logging for risky actions.

## Tech stack

Python, FastAPI, SQLite, plain JavaScript front end, and optionally Amazon Bedrock, the Anthropic API or Ollama for reply wording.