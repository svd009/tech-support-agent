"""The tools the model may call, and the rules the harness enforces on every call.

The model chooses WHICH tool to call and with what arguments. The harness decides whether the call is
allowed, injects the employee's identity (the model never supplies it), runs the real operation, and
returns a compact observation. Rules enforced here, whatever the model says:

  * Identity comes from the signed-in session, never from tool arguments.
  * apply_fix only runs an allow-listed action that a failed check of THIS employee currently lists.
  * Every fix is followed by an automatic re-check; the model receives the verification, not a promise.
  * create_ticket needs diagnostics for diagnosable areas, and is refused while an automatic fix is untried.
  * mark_resolved needs the employee's own confirmation and passing checks.
  * Password reset links go to the employee only; the model never sees or repeats the link.
"""
import json
import re
import time

import playbooks as pb
from classifier import followup_signal
from it_systems import SOFTWARE_CATALOG, ACTIONS, diagnose, employee_devices, lookup_software, run_action, service_status
from retrieval import search_knowledge_base
from tickets import get_ticket, list_tickets

AREAS = ["vpn", "email", "network", "account", "device"]
TICKET_AREAS = AREAS + ["software", "hardware", "security", "other"]
FIX_ACTIONS = [a for a in ACTIONS if a not in {"install_software", "revoke_sessions"}]

TOOLS = [
    {"name": "check_service_status",
     "description": "Company-wide status of vpn, email or network (tells an outage from a personal problem).",
     "parameters": {"type": "object", "properties": {"service": {"type": "string", "enum": ["vpn", "email", "network"]}},
                    "required": ["service"]}},
    {"name": "run_diagnostics",
     "description": "Run health checks for one area on this employee's account and device. vpn also covers network, password and certificate. account = lock and password state.",
     "parameters": {"type": "object", "properties": {"area": {"type": "string", "enum": AREAS}}, "required": ["area"]}},
    {"name": "apply_fix",
     "description": "Apply the fix named in a failed check's 'fix' field. The system re-checks automatically.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": FIX_ACTIONS}}, "required": ["action"]}},
    {"name": "install_software",
     "description": "Install an approved application on the employee's laptop.",
     "parameters": {"type": "object", "properties": {"application": {"type": "string"}}, "required": ["application"]}},
    {"name": "send_password_reset_link",
     "description": "Unlock the account if needed and send the employee a private one-time reset link. Never ask for passwords in chat.",
     "parameters": {"type": "object", "properties": {"area": {"type": "string", "enum": AREAS}}}},
    {"name": "search_knowledge",
     "description": "Search IT how-to articles and troubleshooting steps.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "create_ticket",
     "description": "Hand over to a human technician. Write title and reason for the technician in third person ('the employee'). The system attaches all evidence.",
     "parameters": {"type": "object", "properties": {
         "area": {"type": "string", "enum": TICKET_AREAS}, "title": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["area", "title", "reason"]}},
    {"name": "secure_account",
     "description": "Suspected compromise: sign out all sessions, open a Security ticket, send a reset link.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "mark_resolved",
     "description": "Close an issue after the employee confirms it works and checks pass.",
     "parameters": {"type": "object", "properties": {"area": {"type": "string", "enum": TICKET_AREAS}}, "required": ["area"]}},
    {"name": "lookup",
     "description": "Look up the employee's devices, open tickets, or one ticket.",
     "parameters": {"type": "object", "properties": {"what": {"type": "string", "enum": ["devices", "tickets", "ticket"]},
                                                     "ticket_id": {"type": "integer"}}, "required": ["what"]}},
]
SPEC = {t["name"]: t for t in TOOLS}


class AgentContext(pb.Context):
    """Playbook context plus the bookkeeping the loop needs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocks: list[str] = []          # harness-written text appended after the model's reply
        self.must_escalate: dict[str, tuple[str, str]] = {}  # area -> (title, reason) the harness will ticket
        self.link_sent = False
        self.fix_applied = False
        self.actions_ok = False  # a tool really changed something this turn (fix, install, unlock, revoke)
        self.resolved = False
        self.diagnosed: set[str] = set()
        self.attempted: set[str] = set()
        self.security_done = False
        self.model_calls = 0
        self.model_error = None
        self.reasoning_leaked = False
        self.budget_exhausted = False
        self.password_verified = False
        self.usage = {"input_tokens": 0, "output_tokens": 0}


# ------------------------------------------------------------------ helpers
def peek(ctx: pb.Context, area: str) -> dict | None:
    """Read an issue without creating it or adding the current message to its reports."""
    return ctx.state["issues"].get(area)


def failing(findings: list[dict]) -> list[dict]:
    return [c for c in findings if not c["ok"]]


def _tried(issue: dict, action: str) -> bool:
    return any(a["action"] == action for a in issue["actions"])


def human_needed(issue: dict) -> list[dict]:
    """Failed checks only a person can fix: technician-owned, or an automatic fix the system rejected."""
    return [c for c in failing(issue["findings"])
            if c["owner"] == "technician" or (c["owner"] == "agent" and c["fix"] and _tried(issue, c["fix"]))]


def _slim(findings: list[dict]) -> dict:
    bad = failing(findings)
    out = {"failed": [{"check": c["check"], "problem": c["detail"], "fix": c["fix"],
                       "who_must_act": {"agent": "you, via apply_fix", "employee": "the employee (password reset link)",
                                        "technician": "a human technician (create_ticket)"}[c["owner"]]} for c in bad],
           "passed": [c["check"] for c in findings if c["ok"]]}
    if not bad:
        out["all_passed"] = True
        out["note"] = ("Automated checks cannot see application settings, add-ins, local software, printers or peripherals. "
                       "Ask for the exact error text or search_knowledge before escalating.")
    return out


_SECOND_PERSON = re.compile(r"\b(you|your|yours|you're|you've|you'll|yourself)\b", re.I)


def _for_technician(text: str, area: str, issue: dict, title: bool = False) -> str:
    """Tickets are written for the technician in third person. If the model wrote to the employee ("you"), use a
    harness-written text built from the facts instead of guessing at pronouns."""
    if not _SECOND_PERSON.search(text):
        return text
    if title:
        return f"{pb.DOMAIN_LABEL.get(area, 'Support')}: unresolved after automated checks"
    bad = failing(issue["findings"])
    detail = ("Failing checks: " + "; ".join(f"{c['check']} ({c['detail']})" for c in bad) + ". ") if bad else "All automated checks pass. "
    return detail + "The agent ran the diagnostics and fixes listed in this ticket but could not resolve the employee's problem."


def _error(message: str, **extra) -> dict:
    return {"error": message, **extra}


# ------------------------------------------------------------------ handlers
def check_service_status(ctx, a):
    status = service_status(a["service"])
    return {"service": a["service"], "status": status,
            "meaning": "Healthy company-wide." if status == "operational" else "A company-wide incident; it is not caused by the employee's device."}


def run_diagnostics(ctx, a):
    area = a["area"]
    issue = ctx.issue(area)
    issue["attempts"] += 1
    findings = diagnose(area, ctx.employee["id"])
    issue["findings"] = findings
    ctx.diagnosed.add(area)
    return {"area": area, **_slim(findings)}


def apply_fix(ctx, a):
    action = a["action"]
    area = next((ar for ar in AREAS if (peek(ctx, ar) and any(
        c["fix"] == action and c["owner"] == "agent" for c in failing(peek(ctx, ar)["findings"])))), None)
    if area is None:
        return _error("Not allowed: no failed check of this employee currently lists this fix. Run run_diagnostics first and use a fix it names.")
    if action in ctx.attempted:
        return _error("This fix was already attempted in this conversation turn. Do not repeat it; if the check still fails, a technician is needed.")
    ctx.attempted.add(action)
    issue = ctx.issue(area)
    result = run_action(ctx.employee["id"], ctx.session_id, action)
    issue["actions"].append(result)
    after = diagnose(area, ctx.employee["id"])
    issue["findings"] = after
    ctx.diagnosed.add(area)
    ctx.fix_applied = ctx.fix_applied or result["ok"]
    ctx.actions_ok = ctx.actions_ok or result["ok"]
    out = {"action": action, "applied": result["ok"], "detail": result["detail"],
           "verification": {"area": area, **_slim(after)}}
    if not result["ok"]:
        out["note"] = "The target system rejected this change. A technician is needed for it (create_ticket)."
    return out


def install_software(ctx, a):
    package, name, needs_approval = lookup_software(a["application"])
    issue = ctx.issue("software")
    if not package:
        approved = ", ".join(sorted(SOFTWARE_CATALOG.values()))
        return {"installed": False, "reason": "Not in the approved catalogue and not a known licensed app.",
                "approved_catalogue": approved,
                "next": "If the employee confirms the exact application, create_ticket(area='software') for licence/approval review."}
    if needs_approval:
        ctx.must_escalate["software"] = (f"Software request: {name}",
                                         f"{name} needs a paid licence or manager approval before installation; automation is not permitted.")
        return {"installed": False, "application": name, "reason": "Needs licence or manager approval. A technician ticket will be raised for it."}
    result = run_action(ctx.employee["id"], ctx.session_id, "install_software", package=package)
    issue["actions"].append(result)
    if not result["ok"]:
        ctx.must_escalate["software"] = (f"Software install failed: {name}", result["detail"])
        return {"installed": False, "application": name, "reason": result["detail"]}
    from it_systems import get_state
    installed = package in get_state(ctx.employee["id"])["software"]["installed"]
    ctx.fix_applied = ctx.fix_applied or installed
    ctx.actions_ok = ctx.actions_ok or installed
    return {"installed": installed, "application": name, "verified": installed,
            "note": "It appears in the Start menu within a few minutes."}


def send_password_reset_link(ctx, a):
    if ctx.link_sent:
        return {"sent": True, "note": "A link was already sent in this turn."}
    if ctx.state.get("links_issued", 0) >= 3:
        ctx.must_escalate["account"] = ("Corporate account: self-service password reset not completed",
                                        "Three reset links were issued in this conversation without a completed reset.")
        return _error("Reset link limit reached for this conversation. A technician ticket will be raised.")
    account = ctx.issue("account")
    checks = diagnose("account", ctx.employee["id"])
    unlocked = False
    if any(c["fix"] == "unlock_account" for c in failing(checks)):
        result = run_action(ctx.employee["id"], ctx.session_id, "unlock_account")
        account["actions"].append(result)
        unlocked = result["ok"]
        ctx.actions_ok = ctx.actions_ok or unlocked
        checks = diagnose("account", ctx.employee["id"])
    account["findings"] = checks
    resume = a.get("area") or "account"
    ctx.blocks.append(pb.send_password_link(ctx, resume))  # the link is only ever shown to the employee
    ctx.state["links_issued"] = ctx.state.get("links_issued", 0) + 1
    ctx.link_sent = True
    return {"sent": True, "account_unlocked": unlocked,
            "note": ("The system has sent the employee a private one-time link with steps and will show them in this reply. "
                     "Tell them to open it, set a new password, then reply 'done'. Do not write the link or any password.")}


def search_knowledge(ctx, a):
    result = search_knowledge_base(a["query"])
    ctx.state["kb_searched"] = True
    if not result["results"]:
        return {"found": False, "note": "No matching article."}
    top = result["results"][0]
    ctx.sources.add(top["source"])
    return {"found": True, "source": top["source"], "content": top["content"][:1100]}


def create_ticket(ctx, a):
    area, title, reason = a["area"], a["title"][:100], a["reason"][:600]
    issue = ctx.issue(area)
    reason = _for_technician(reason, area, issue)
    title = _for_technician(title, area, issue, title=True)
    if area in AREAS:
        if not issue["findings"]:
            return _error(f"Run run_diagnostics(area='{area}') first. Tickets for this area must include diagnostic evidence.")
        untried = [c for c in failing(issue["findings"]) if c["owner"] == "agent" and c["fix"] and not _tried(issue, c["fix"])]
        if untried:
            c = untried[0]
            return _error(f"The check '{c['check']}' can be fixed automatically. Call apply_fix(action='{c['fix']}') before escalating.")
        no_findings = not failing(issue["findings"])
        if no_findings and not (ctx.state.get("kb_searched") or ctx.state.get("asked_question") or issue.get("gate_hits")):
            issue["gate_hits"] = 1
            return _error("All automated checks pass, so the cause is outside what they can see. Before escalating, either search_knowledge "
                          "for steps matching the symptom, or ask the employee for the exact error message and what they see. "
                          "If that does not help, call create_ticket again.")
    needed = human_needed(issue) if area in AREAS else []
    category = next((c["category"] for c in needed if c["category"] not in {None, "other"}), None)
    filed = pb.file_ticket(ctx, area, title, reason, category)
    if filed is None:
        return {"ticket": None, "declined": True,
                "note": "The employee asked for no ticket. Everything is saved as a draft. Tell them to say 'create a ticket' to send it."}
    ticket, created = filed
    ctx.must_escalate.pop(area, None)
    return {"ticket_id": ticket["id"], "created": created, "complexity": ticket["complexity"],
            "note": "The system adds the ticket number and target time to your reply. Do not invent ticket details."}


def secure_account(ctx, a):
    if ctx.security_done:
        return {"done": True, "note": "Already handled in this turn."}
    ctx.security_done = True
    issue = ctx.issue("security")
    revoke = run_action(ctx.employee["id"], ctx.session_id, "revoke_sessions", now=int(time.time()))
    issue["actions"].append(revoke)
    ctx.actions_ok = ctx.actions_ok or revoke["ok"]
    filed = pb.file_ticket(ctx, "security", "Security: possible account compromise / suspicious activity",
                           a["reason"][:500] or "Possible compromise. The Security team must review sign-in logs.", "security_review")
    ctx.blocks.insert(0, "Security first: do NOT approve any MFA prompt you didn't start, never share a code with anyone (IT will never ask), "
                         "and don't click links or open attachments in the suspicious message.\n"
                         + ("I signed your account out of every device and revoked active sessions."
                            if revoke["ok"] else "I could not revoke your sessions automatically."))
    if not ctx.link_sent:
        ctx.blocks.append("As a precaution, please change your password now.\n\n" + pb.send_password_link(ctx, "account"))
        ctx.state["links_issued"] = ctx.state.get("links_issued", 0) + 1
        ctx.link_sent = True
    return {"sessions_revoked": revoke["ok"], "ticket_id": filed[0]["id"] if filed else None,
            "note": "Sessions revoked, Security ticket handled and a reset link sent. The system adds these details to your reply."}


def mark_resolved(ctx, a):
    area = a["area"]
    if followup_signal(ctx.text) != "positive":
        return _error("The employee has not confirmed in this message that it works. Ask them to try it and reply, then call this again.")
    issue = peek(ctx, area)
    if issue is None:
        return _error("There is no open issue in this area.")
    if area in AREAS:
        current = diagnose(area, ctx.employee["id"])
        issue["findings"] = current
        if failing(current):
            return _error("Checks still fail: " + "; ".join(c["check"] for c in failing(current)) + ". The issue is not resolved.")
    elif not any(act["ok"] for act in issue["actions"]):
        return _error("Nothing was done for this issue yet, so it cannot be marked resolved.")
    issue["resolved"] = True
    ctx.resolved = True
    return {"resolved": True}


def lookup(ctx, a):
    what = a["what"]
    if what == "devices":
        return {"devices": employee_devices(ctx.employee["id"])}
    if what == "ticket":
        try:
            t = get_ticket(int(a.get("ticket_id", 0)), ctx.employee)
        except Exception:
            return _error("No such ticket on this employee's account.")
        return {"ticket_id": t["id"], "title": t["title"], "status": t["status"], "target": t.get("resolution_estimate")}
    tickets = [t for t in list_tickets(ctx.employee) if t["status"] != "resolved"][:10]
    return {"open_tickets": [{"id": t["id"], "status": t["status"], "title": t["title"]} for t in tickets]}


HANDLERS = {fn.__name__: fn for fn in (check_service_status, run_diagnostics, apply_fix, install_software,
                                       send_password_reset_link, search_knowledge, create_ticket, secure_account,
                                       mark_resolved, lookup)}


# ------------------------------------------------------------------ dispatch
def validate(name: str, args) -> tuple[dict, str | None]:
    """Check arguments against the tool's schema. Unknown arguments (such as an employee id) are dropped."""
    spec = SPEC.get(name)
    if spec is None:
        return {}, f"Unknown tool '{name}'. Available: {', '.join(SPEC)}."
    if not isinstance(args, dict):
        return {}, "Arguments must be a JSON object."
    props = spec["parameters"]["properties"]
    clean = {}
    for key, rule in props.items():
        if key not in args or args[key] in (None, ""):
            continue
        value = args[key]
        if rule.get("type") == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return {}, f"'{key}' must be an integer."
        else:
            value = str(value)
        if "enum" in rule and value not in rule["enum"]:
            return {}, f"'{key}' must be one of: {', '.join(rule['enum'])}."
        clean[key] = value
    missing = [k for k in spec["parameters"].get("required", []) if k not in clean]
    if missing:
        return {}, f"Missing required argument(s): {', '.join(missing)}."
    return clean, None


def execute(ctx: AgentContext, name: str, args) -> dict:
    clean, problem = validate(name, args)
    if problem:
        observation = _error(problem)
    else:
        try:
            observation = HANDLERS[name](ctx, clean)
        except Exception as exc:  # a tool fault must not crash the turn; the model sees it and can adapt
            observation = _error(f"The tool failed ({type(exc).__name__}). Treat this check as unavailable.")
    shown = clean if clean else (args if isinstance(args, dict) else {})
    ctx.tool_results.append({"tool": name, "arguments": shown, "result": observation})
    return observation


def tool_schemas() -> list[dict]:
    """What the model sees. The long list of fix names is dropped (the failed check already names its fix and
    validate() still enforces the list), which saves prompt tokens on every step."""
    compact = []
    for tool in TOOLS:
        copy = json.loads(json.dumps(tool))
        copy["parameters"]["properties"].get("action", {}).pop("enum", None)
        compact.append(copy)
    return compact


def observation_text(observation: dict) -> str:
    return json.dumps(observation, ensure_ascii=False, separators=(',', ':'))[:2000]
