"""Per-issue playbooks: diagnose -> fix what the agent is allowed to fix -> verify -> escalate.

Each playbook receives a Context and returns nothing; it writes the answer, outcome, tool
evidence and next expected reply into the Context. All decisions are code, not model output.
"""
import re
import time
from datetime import datetime

from auth import issue_directory_reset
from it_systems import (SOFTWARE_CATALOG, diagnose, employee_devices, get_state, lookup_software,
                        run_action, service_status)
from retrieval import search_knowledge_base
from support_policy import POLICY
from tickets import escalate_issue, get_ticket, list_tickets

DOMAIN_LABEL = {"vpn": "VPN", "email": "Email", "network": "Network", "account": "Corporate account",
                "device": "Device compliance", "software": "Software", "hardware": "Hardware",
                "security": "Security", "safety": "Unsafe equipment", "other": "Support request"}
DEFAULT_CATEGORY = {"vpn": "vpn_connectivity", "email": "email_service", "network": "network_incident",
                    "account": "account_access", "device": "device_compliance", "software": "admin_install",
                    "hardware": "hardware_repair", "security": "security_review", "safety": "hardware_repair",
                    "other": "other"}
RETRY_HINT = {
    "vpn": "Please disconnect, fully quit the VPN app, reopen it and connect again.",
    "email": "Please close and reopen Outlook (allow a minute for it to sync).",
    "network": "Please check whether websites load now.",
    "device": "Your device may show 'compliant' after its next check-in (up to 15 minutes); a restart speeds this up.",
    "software": "It will appear in your Start menu within a few minutes.",
}


class Context:
    def __init__(self, employee: dict, session_id: str, text: str, state: dict, declined: bool):
        self.employee, self.session_id, self.text, self.state = employee, session_id, text, state
        self.declined = declined
        self.answer = ""
        self.outcome = "completed"
        self.tool_results: list[dict] = []
        self.created_tickets: list[dict] = []
        self.sources: set[str] = set()
        self.secret_links: list[str] = []  # shown once to the employee, redacted in stored history
        self.first_turn = True  # False once the conversation has started: no repeated greeting
        self.verbatim = False  # True: the answer is fixed text (safety, lists) and the model must not reword it

    # --- helpers -------------------------------------------------------------
    def issue(self, domain: str) -> dict:
        issue = self.state["issues"].setdefault(domain, {"reports": [], "actions": [], "findings": [],
                                                         "attempts": 0, "ticket_id": None, "resolved": False})
        if self.text and self.text not in issue["reports"]:
            issue["reports"].append(self.text[:1000])
        return issue

    def record(self, tool: str, result: dict):
        self.tool_results.append({"tool": tool, "result": result})
        import progress
        progress.add(self.employee["id"], _progress_label(tool, result))

    def expect(self, kind: str | None, domain: str | None = None, **extra):
        self.state["awaiting"] = {"type": kind, "domain": domain, **extra} if kind else None


# ------------------------------------------------------------------ shared steps
# When every automated check passes, the cause is something the checks cannot see. Before handing over to a
# technician the agent suggests one safe step and asks one targeted question; the answer goes into the ticket.
PROBE = {
    "vpn": ("disconnect, fully quit the VPN app and reconnect, ideally once on a different network such as a phone hotspot",
            "Does it happen on every network or only one (home Wi-Fi, office, hotspot), and what exact message do you see?"),
    "email": ("close the app completely, then open Outlook on the web (outlook.office.com) and see whether it works there",
              "Which Microsoft 365 app is affected (Outlook, Teams, Word, Excel, OneDrive), and what does it show when it stops responding?"),
    "network": ("turn Wi-Fi off and on again, or restart the laptop",
                "Is it every website or only some, and does it also happen on another network?"),
    "device": ("restart the laptop so it checks in with the management service again",
               "What is the exact text of the warning or pop-up, and when does it appear?"),
    "slow": ("restart the laptop (a full restart, not sleep) and close apps you aren't using",
             "Is everything slow or one app in particular, and since when (for example after an update)?"),
}
_STILL_A_PROBLEM = (r"\b(but|however|although|though|eventually|continues?|keeps?|comes? back|returns?|recurs?|"
                  r"slow\w*|lag\w*|freez\w*|crash\w*|drop\w*|disconnect\w*|sometimes|at times|"
                  r"for (some ?time|a while|a bit|an hour)|starts? (to )?\w+ing)\b")


def says_fixed(text: str, signal: str | None = None) -> bool:
    """True only when the employee reports that it works now. Mixed reports ("works after a restart but slows
    down again") and long descriptions are problem reports, not confirmations."""
    t = text.lower()
    if signal == "negative" or len(t.split()) > 25 or re.search(_STILL_A_PROBLEM, t):
        return False
    return bool(re.search(WORKS_NOW, t))


WORKS_NOW = r"\b(works( again| now)?|it work(ed|s)|working( again| now)?|fixed|resolved|sorted|all good|good now|that did it)\b"


UNINFORMATIVE = r"^\W*(idk|i don'?t know|dunno|no idea|not sure|hello|hi|hey|\?+|ok|okay|hmm+|\.+)\W*$"


def _uninformative(text: str) -> bool:
    import re
    return bool(re.match(UNINFORMATIVE, (text or "").strip().lower()))


def _progress_label(tool: str, result: dict) -> str:
    if tool == "route_message":
        return f"Understood your message ({result.get('intent')}, by {result.get('source')})"
    if tool.startswith(("diagnose_", "verify_")) and "checks" in result:
        failed = sum(1 for c in result["checks"] if not c["ok"])
        verb = "Re-checked" if tool.startswith("verify_") else "Checked"
        area = DOMAIN_LABEL.get(tool.split("_", 1)[1], tool.split("_", 1)[1])
        return f"{verb} {area}: " + (f"{failed} problem(s) found" if failed else "all checks pass")
    if tool == "escalate_support_issue":
        return f"Sent ticket #{result.get('ticket_id')} to the Tech Support team"
    if tool == "issue_password_reset_link":
        return "Generated a one-time password reset link"
    if isinstance(result, dict) and "detail" in result:
        return ("Fix applied: " if result.get("ok") else "Fix was rejected: ") + str(result["detail"])
    return tool.replace("_", " ").capitalize()


def _fmt_checks(checks: list[dict]) -> str:
    return "\n".join(f"  [{'OK' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}" for c in checks)


def _summary(checks: list[dict], label: str) -> str:
    """Employee-facing: name only the failures; the ticket keeps the full list."""
    failed = [c for c in checks if not c["ok"]]
    if not failed:
        return f"All {len(checks)} {label} checks pass."
    return (f"{len(checks) - len(failed)} of {len(checks)} {label} checks pass. Still failing:\n"
            + "\n".join(f"  • {c['check']}: {c['detail']}" for c in failed))


def _fmt_actions(actions: list[dict]) -> str:
    return "\n".join(f"  [{'done' if a['ok'] else 'FAILED'}] {a['detail']}" for a in actions) or "  (none)"


def _profile_line(employee_id: str) -> str:
    from database import connect
    with connect() as db:
        row = db.execute("SELECT * FROM employee_profiles WHERE employee_id=?", (employee_id,)).fetchone()
    if not row:
        return "No profile on record"
    return f"{row['title']}, {row['team']}, {row['location']} (manager: {row['manager_id'] or 'n/a'}, started {row['start_date']})"


def _device_line(employee_id: str) -> str:
    devices = employee_devices(employee_id)
    if not devices:
        return "No managed device on record"
    return "; ".join(f"{d['id']} ({d['name']}, {d['operating_system']}, {'compliant' if d['compliant'] else 'NOT compliant'})"
                     for d in devices)


def file_ticket(ctx: Context, domain: str, title: str, reason: str, category: str | None = None,
                employee_reason: str | None = None):
    """Create (or update) the technician ticket holding everything the agent learned.

    Returns (ticket, created), or None when the employee declined tickets (the request is parked
    in session state so "create a ticket" can send it later).
    """
    issue = ctx.issue(domain)
    category = category or DEFAULT_CATEGORY.get(domain, "other")
    description = "\n".join([
        f"Reported by: {ctx.employee['name']} ({ctx.employee['id']})",
        f"Role: {_profile_line(ctx.employee['id'])}",
        f"Device: {_device_line(ctx.employee['id'])}",
        f"Area: {DOMAIN_LABEL.get(domain, domain)}",
        f"Conversation ID: {ctx.session_id}",
        f"Escalated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Employee's description:",
        *[f'  "{line}"' for line in issue["reports"]],
        "",
        "Automated diagnostics (latest run):",
        _fmt_checks(issue["findings"]) if issue["findings"] else "  (no automated diagnostics for this area)",
        "",
        "Automated actions already taken by the agent:",
        _fmt_actions(issue["actions"]),
        "",
        "Why a technician is needed:",
        f"  {reason}",
    ])[:6000]
    if ctx.declined:
        ctx.state["pending_escalation"] = {"domain": domain, "title": title, "reason": reason, "category": category,
                                           "employee_reason": employee_reason}
        return None
    result = escalate_issue(ctx.employee["id"], ctx.session_id, title, description, category, reason)
    ticket = result["ticket"]
    issue["ticket_id"] = ticket["id"]
    ctx.state.pop("pending_escalation", None)
    ctx.created_tickets.append(ticket)
    ctx.record("escalate_support_issue", {"ticket_id": ticket["id"], "created": result["created"],
                                          "category": category, "title": title})
    return ticket, result["created"]


def _includes(issue: dict) -> str:
    parts = ["your description", "your device"]
    if issue["findings"]:
        parts.append("every check I ran")
    if issue["actions"]:
        parts.append("every fix I attempted")
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def escalate(ctx: Context, domain: str, title: str, reason: str, category: str | None = None,
             lead: str = "", opener: str | None = None, employee_reason: str | None = None) -> None:
    """Playbook helper: file the ticket and write the employee-facing answer."""
    issue = ctx.issue(domain)
    filed = file_ticket(ctx, domain, title, reason, category, employee_reason)
    if filed is not None:
        ctx.state.pop("human_requested", None)
    if filed is None:
        ctx.answer = (lead + ("\n\n" if lead else "") + f"I can't fix this automatically: {employee_reason or reason}\n\n"
                      "You asked me not to create a ticket, so I haven't. Say \"create a ticket\" whenever you "
                      "want me to send everything above to the Tech Support team.")
        ctx.outcome = "escalation_declined"
        ctx.expect(None)
        return
    ticket, created = filed
    category = category or DEFAULT_CATEGORY.get(domain, "other")
    ctx.outcome = "escalated"
    ctx.expect("escalated", domain, ticket_id=ticket["id"], title=title, category=category)
    if not created:
        ctx.answer = (lead + ("\n\n" if lead else "") +
                      f"I've added this to your existing ticket #{ticket['id']}, so the technician sees your update. "
                      "You can follow it under My tickets.")
        return
    verb = "raised"
    ctx.answer = (lead + ("\n\n" if lead else "") +
                  (opener or f"I couldn't {'fully ' if any(a['ok'] for a in issue['actions']) else ''}fix this automatically, so I've {{verb}} ticket #{{id}} for the Tech Support team.").format(verb=verb, id=ticket["id"]) + "\n"
                  f"Reason: {employee_reason or reason}\n\n"
                  f"The ticket already includes {_includes(issue)}, so a technician won't need to ask you to repeat anything.\n"
                  f"Initial complexity: {ticket['complexity']}. Provisional target: {ticket['resolution_estimate']} "
                  "(a technician confirms timing).")


def send_password_link(ctx: Context, domain: str) -> str:
    link = issue_directory_reset(ctx.employee["id"], ctx.session_id)
    ctx.secret_links.append(link["reset_path"])
    ctx.record("issue_password_reset_link", {"expires_in_seconds": link["expires_in"], "one_use": True,
                                             "link": "[shown to employee once; not stored]"})
    expires = datetime.fromtimestamp(time.time() + link["expires_in"]).strftime("%H:%M")
    ctx.expect("password_done", domain, issued_at=int(time.time()))
    return (f"Reset link (one use, valid until {expires}): {link['reset_path']}\n\n"
            "Steps:\n"
            "  1. Open the link above (button below this message).\n"
            "  2. Enter a new password: at least 12 characters with three of uppercase, lowercase, digits, symbols. "
            "Don't reuse an old one.\n"
            "  3. Type it again to confirm and press Set new password.\n"
            "  4. Come back here and reply \"done\". I'll verify the change on the directory.\n"
            "  5. Afterwards, sign in again on your laptop, VPN and phone mail app with the new password.\n"
            "Never type your password into this chat.")


def fix_domain(ctx: Context, domain: str, after_employee_step: bool = False) -> None:
    """The core loop for any domain with automated diagnostics."""
    issue = ctx.issue(domain)
    issue["attempts"] += 1
    label = DOMAIN_LABEL[domain]
    before = diagnose(domain, ctx.employee["id"])
    ctx.record(f"diagnose_{domain}", {"checks": before})
    failed = [c for c in before if not c["ok"]]

    actions = []
    for check in failed:
        if check["owner"] == "agent" and check["fix"]:
            result = run_action(ctx.employee["id"], ctx.session_id, check["fix"])
            ctx.record(check["fix"], result)
            actions.append(result)
    issue["actions"].extend(actions)
    after = diagnose(domain, ctx.employee["id"]) if actions else before
    if actions:
        ctx.record(f"verify_{domain}", {"checks": after})
    issue["findings"] = after
    remaining = [c for c in after if not c["ok"]]
    technician = [c for c in remaining if c["owner"] in {"technician", "agent"}]  # agent-owned + still failing = fix rejected
    employee_steps = [c for c in remaining if c["owner"] == "employee"]

    done_text = ("What I found and fixed:\n" + "\n".join(f"  • {a['detail']}" for a in actions if a["ok"])) \
        if any(a["ok"] for a in actions) else ""

    if technician:
        first = technician[0]
        outage = first["category"] == "service_outage"
        reason = (f"{first['check']} - {first['detail']}." if not outage else
                  f"There is an active {label} service incident ({first['detail'].lower()}). It is not caused by your device.")
        if first["owner"] == "agent":
            reason = f"Automatic fix for '{first['check']}' was rejected by the system: " + \
                     next((a["detail"] for a in actions if a["action"] == first["fix"]), first["detail"])
        lead = done_text
        if employee_steps:
            lead += ("\n\n" if lead else "") + "Your corporate password has also expired; a technician will help with that too."
        escalate(ctx, domain, f"{label}: {first['detail']}"[:150], reason,
                 category=first["category"] if first["category"] != "other" else None, lead=lead)
        return

    if employee_steps:
        ctx.answer = ((done_text + "\n\n") if done_text else "") + \
            f"I found the cause: your corporate password has expired, which blocks {label} sign-in. " \
            "For security only you can choose the new password, so I've generated a personal reset link.\n\n" + \
            send_password_link(ctx, domain)
        ctx.outcome = "awaiting_employee_action"
        return

    if actions:
        ctx.answer = (f"{done_text}\n\nI re-checked everything: {_summary(after, label)}\n\n"
                      f"{RETRY_HINT.get(domain, 'Please try again.')} Does it work now? Reply \"yes\" or \"still not working\".")
        ctx.outcome = "fixed_pending_confirmation"
        ctx.expect("confirm_fix", domain)
        return

    if after_employee_step:
        ctx.answer = (f"Confirmed: your password change is recorded. {_summary(after, label)}\n\n"
                      f"{RETRY_HINT.get(domain, 'Please try again.')} Sign in with your NEW password. "
                      "Does it work now? Reply \"yes\" or \"still not working\".")
        ctx.outcome = "fixed_pending_confirmation"
        ctx.expect("confirm_fix", domain)
        return

    summary = _summary(after, label)
    if not issue["actions"] and not issue.get("probed") and domain in PROBE and not ctx.state.get("human_requested"):
        # Nothing is visibly wrong: suggest one safe step and ask one targeted question before handing over.
        issue["probed"] = True
        step, question = PROBE[issue.get("probe_key") or domain]
        ctx.answer = (f"I ran a full {label} diagnostic on your account, your device and the service. {summary} "
                      "I haven't changed anything, because nothing I can check is broken, so the cause is "
                      "something my checks can't see.\n\n"
                      f"One thing to try: {step}.\n\n"
                      f"To pin it down: {question}\n\n"
                      "If it still fails after that, your answer goes straight into a ticket so a technician "
                      "doesn't have to ask you again.")
        ctx.outcome = "needs_clarification"
        ctx.expect("probe", domain)
        return

    answered = bool(issue.get("probed"))
    if answered and not issue["actions"] and _uninformative(ctx.text) and not issue.get("nudged") and not issue.get("ticket_id"):
        issue["nudged"] = True
        step, question = PROBE[issue.get("probe_key") or domain]
        ctx.answer = ("No problem. Even a small detail helps a technician, so if you can: " + question[0].lower() + question[1:] +
                      "\n\nIf you'd rather skip it, say \"create a ticket\" and I'll send it as it is.")
        ctx.outcome = "needs_clarification"
        ctx.expect("probe", domain)
        return
    if issue.get("ticket_id") and not actions:
        # The employee is following up on a problem that already has a ticket: add the update, don't start over.
        escalate(ctx, domain, f"{label} issue unresolved after automated checks"[:150],
                 "The employee added a follow-up; see the employee's description.",
                 lead="Understood, thanks for the update. I re-checked and nothing new has changed on my side.")
        return
    if issue["actions"]:
        reason = ("The employee reports the problem persists after the automated fixes, and all automated checks now pass. "
                  "Needs hands-on investigation.")
        employee_reason = "Everything I can check now looks healthy, so a technician needs to look at it directly."
        lead = "I'm sorry it's still not working. I re-checked everything after the fix and it all looks healthy on my side."
    elif answered:
        reason = (f"All automated {label} checks pass. The agent suggested one self-service step and asked a targeted "
                  "question; the employee's reply is under the employee's description. The cause is outside what "
                  "the agent can inspect or change, for example local software, application settings or a server-side fault.")
        employee_reason = (f"Everything I can check on the {label} side passes, so the cause is outside what I can "
                           "inspect or change (for example local software, application settings or a server-side fault).")
        lead = (("Thanks, I've added your answer to the ticket. " if not ctx.declined else "Thanks, that helps. ") +
                "My checks still all pass, so this needs a technician to look at it directly." if not _uninformative(ctx.text) else
                "Thanks. I don't have more detail, so I'll pass it on as it is. My checks all pass, so this needs a "
                "technician to look at it directly.")
    else:
        reason = (f"All automated {label} checks pass, so the cause is outside what the agent can inspect or change "
                  "(for example local software, application settings or a server-side fault).")
        employee_reason = None
        lead = f"I ran a full {label} diagnostic on your account, device and the service. {summary}"
    escalate(ctx, domain, f"{label} issue unresolved after automated checks"[:150], reason,
             lead=lead, employee_reason=employee_reason)


# ------------------------------------------------------------------ playbooks
def password_playbook(ctx: Context) -> None:
    issue = ctx.issue("account")
    issue["attempts"] += 1
    checks = diagnose("account", ctx.employee["id"])
    ctx.record("diagnose_account", {"checks": checks})
    issue["findings"] = checks
    notes = []
    if any(c["fix"] == "unlock_account" for c in checks if not c["ok"]):
        result = run_action(ctx.employee["id"], ctx.session_id, "unlock_account")
        ctx.record("unlock_account", result)
        issue["actions"].append(result)
        if not result["ok"]:
            escalate(ctx, "account", "Corporate account: automatic unlock rejected", "The identity system rejected the automated unlock.")
            return
        notes.append(f"• {result['detail']}.")
    expired = any(c["fix"] == "issue_password_reset" for c in checks if not c["ok"])
    notes.append("• Your password has expired." if expired else "• Your account is in good standing.")
    ctx.answer = ("I checked your corporate account:\n" + "\n".join(notes) + "\n\n"
                  "Here is your self-service reset. It changes your corporate password used for Windows, VPN and email.\n\n"
                  + send_password_link(ctx, "account"))
    ctx.outcome = "awaiting_employee_action"


def password_done(ctx: Context, awaiting: dict) -> None:
    """Employee says they finished the reset: verify it on the directory, then re-check the original problem."""
    domain = awaiting.get("domain") or "account"
    changed_at = get_state(ctx.employee["id"])["directory"].get("password_changed_at") or 0
    ctx.record("verify_password_change", {"changed": changed_at >= awaiting.get("issued_at", 0)})
    if changed_at < awaiting.get("issued_at", 0):
        ctx.answer = ("I don't see a password change on the directory yet. Please open the reset link, set the new "
                      "password and press Set new password, then reply \"done\". If the link no longer works, "
                      "reply \"link not working\" and I'll send a new one.")
        ctx.outcome = "awaiting_employee_action"
        return  # keep waiting
    if domain == "account":
        issue = ctx.issue("account")
        issue["resolved"] = True
        ctx.answer = ("Confirmed: your corporate password was changed and your account is unlocked and active.\n"
                      "Next: sign in again on your laptop, VPN and phone mail app with the new password. "
                      "Anything else I can help with?")
        ctx.outcome = "resolved"
        ctx.expect(None)
        return
    fix_domain(ctx, domain, after_employee_step=True)


def password_problem(ctx: Context, awaiting: dict) -> None:
    issue = ctx.issue("account")
    issue["link_failures"] = issue.get("link_failures", 0) + 1
    if issue["link_failures"] >= 2:
        escalate(ctx, "account", "Corporate account: self-service password reset not completed",
                 "The employee could not complete the self-service password reset after two links.",
                 category="password_reset")
        return
    ctx.answer = "Sorry about that. Here is a fresh link (the old one no longer works).\n\n" + \
        send_password_link(ctx, awaiting.get("domain") or "account")
    ctx.outcome = "awaiting_employee_action"


def software_playbook(ctx: Context, text: str) -> None:
    package, name, needs_approval = lookup_software(text)
    if not package:
        if ctx.state.get("awaiting", {}) and ctx.state["awaiting"].get("type") == "clarify":
            escalate(ctx, "software", "Software request: application not in approved catalogue",
                     "The requested application is not in the approved catalogue and needs review.")
            return
        ctx.answer = ("Which application do you need? I can install these immediately: "
                      + ", ".join(sorted(SOFTWARE_CATALOG.values()))
                      + ". Anything else goes to a technician for licence/approval review.")
        ctx.outcome = "needs_clarification"
        ctx.expect("clarify", "software", text=text)
        return
    issue = ctx.issue("software")
    if needs_approval:
        escalate(ctx, "software", f"Software request: {name}",
                 f"{name} needs a paid licence or manager approval before installation; automation is not permitted.",
                 category="admin_install",
                 lead=f"{name} isn't on the self-service catalogue because it needs licence/manager approval.",
                 opener="I've {verb} ticket #{id} so a technician arranges approval and installs it.")
        return
    already = package in get_state(ctx.employee["id"])["software"]["installed"]
    result = run_action(ctx.employee["id"], ctx.session_id, "install_software", package=package)
    ctx.record("install_software", result)
    issue["actions"].append(result)
    if not result["ok"]:
        escalate(ctx, "software", f"Software install failed: {name}", result["detail"], category="admin_install")
        return
    installed = package in get_state(ctx.employee["id"])["software"]["installed"]
    ctx.record("verify_software", {"package": package, "installed": installed})
    ctx.answer = ((f"{name} was already installed, so I re-deployed it to repair the installation."
                   if already else f"Done - I've installed {name} on your laptop from the approved catalogue.")
                  + f" {RETRY_HINT['software']} Is it working? Reply \"yes\" or \"still not working\".")
    ctx.outcome = "fixed_pending_confirmation"
    ctx.expect("confirm_fix", "software")


def security_playbook(ctx: Context) -> None:
    issue = ctx.issue("security")
    result = run_action(ctx.employee["id"], ctx.session_id, "revoke_sessions", now=int(time.time()))
    ctx.record("revoke_sessions", result)
    issue["actions"].append(result)
    guidance = ("Security first:\n"
                "  • Do NOT approve any MFA prompt you didn't start, and never share a code with anyone (IT will never ask).\n"
                "  • Don't click links or open attachments in the suspicious message; don't forward it.\n"
                f"  • {'I signed your account out of every device and revoked active sessions.' if result['ok'] else 'I could not revoke your sessions automatically.'}")
    escalate(ctx, "security", "Security: possible account compromise / suspicious activity",
             "Possible compromise. The Security team must review sign-in logs.", category="security_review",
             lead=guidance, opener="I've {verb} ticket #{id} so the Security team reviews your account's sign-in activity.")
    if ctx.outcome == "escalated":
        ctx.answer += "\n\nAs a precaution, please change your password now.\n\n" + send_password_link(ctx, "account")


def safety_playbook(ctx: Context) -> None:
    ctx.verbatim = True
    lead = ("Stop using the equipment now: unplug it only if that is safe, and move away. If there is smoke, "
            "fire or any immediate danger, follow your workplace emergency procedure and call emergency services. "
            "A support ticket is not an emergency response.")
    escalate(ctx, "safety", "Unsafe equipment reported", "Equipment reported as unsafe; it must be inspected and replaced.",
             category="hardware_repair", lead=lead,
             opener="I've {verb} ticket #{id} so a technician inspects and replaces the equipment.")


def hardware_playbook(ctx: Context) -> None:
    escalate(ctx, "hardware", f"Hardware fault: {ctx.text[:110]}",
             "Physical hardware needs inspection, repair or replacement by a technician.",
             lead="This is a physical hardware problem, which I can't repair remotely. If you have a spare "
                  "device or can use a hot desk meanwhile, that's the fastest workaround.",
             opener="I've {verb} ticket #{id} so a technician can repair or replace it.")


def app_playbook(ctx: Context) -> None:
    """A Microsoft 365 desktop app (Excel, Word, OneDrive...) misbehaving. No remote check can see inside the app,
    so the agent says so, suggests one safe step, asks one question, and hands over with the answer if that fails."""
    issue = ctx.issue("software")
    issue["probed"] = True
    if ctx.state.get("human_requested"):
        escalate(ctx, "software", f"Microsoft 365 app problem: {issue['reports'][0][:100]}",
                 "A desktop application or file problem that the agent has no remote check or fix for. "
                 "The employee asked for a technician.", category="other",
                 lead="Understood, I'll hand this to a technician.",
                 employee_reason="You asked for a technician, and desktop apps are outside what I can inspect remotely.")
        return
    ctx.answer = ("I can't see inside desktop apps like Excel, Word or OneDrive from here, so I haven't changed "
                  "anything yet.\n\n"
                  "One thing to try: close the app completely, reopen it, and open a different file to see whether "
                  "the problem is the app or that one file.\n\n"
                  "To pin it down: does it happen with every file or only one, and what exactly do you see "
                  "(a freeze, a crash, or an error message)?\n\n"
                  "If it still fails after that, your answer goes straight into a ticket so a technician doesn't "
                  "have to ask you again.")
    ctx.outcome = "needs_clarification"
    ctx.expect("probe_app", "software")


def app_followup(ctx: Context, text: str, signal: str) -> None:
    import re
    issue = ctx.issue("software")
    if says_fixed(text, signal):
        issue["resolved"] = True
        ctx.answer = "Great, glad it's working again! No ticket was needed. Anything else I can help with?"
        ctx.outcome = "resolved"
        ctx.expect(None)
        return
    if _uninformative(text) and not issue.get("nudged"):
        issue["nudged"] = True
        ctx.answer = ("No problem. Even a small detail helps a technician, so if you can: does it happen with every file "
                      "or only one, and what exactly do you see (a freeze, a crash, or an error message)?\n\n"
                      "If you'd rather skip it, say \"create a ticket\" and I'll send it as it is.")
        ctx.outcome = "needs_clarification"
        ctx.expect("probe_app", "software")
        return
    escalate(ctx, "software", f"Microsoft 365 app problem: {issue['reports'][0][:100]}",
             "A desktop application or file problem that the agent has no remote check or fix for. The agent suggested "
             "a restart and a test with another file and asked a question; the employee's reply is under "
             "the employee's description.", category="other",
             lead="Thanks, I've added your answer to the ticket. I have no way to look inside the app or the file "
                  "from here, so this needs a technician.",
             employee_reason="Desktop apps and files are outside what I can inspect or change remotely.")


def access_playbook(ctx: Context) -> None:
    escalate(ctx, "account", f"Access request: {ctx.text[:110]}",
             "Access to shared folders and systems needs the owner's or a manager's approval; the agent is not "
             "permitted to grant permissions.", category="account_access",
             lead="I can't grant access to folders or systems myself: permissions need approval from the owner "
                  "or your manager, so I haven't changed anything.",
             opener="I've {verb} ticket #{id} so a technician arranges the approval and grants the access.")


def service_status_answer(ctx: Context) -> None:
    ctx.verbatim = True
    lowered = ctx.text.lower()
    names = [n for n in ("vpn", "email", "network") if n in lowered or (n == "email" and ("outlook" in lowered or "mail" in lowered))
             or (n == "network" and "internet" in lowered)] or ["vpn", "email", "network"]
    statuses = {n: service_status(n) for n in names}
    ctx.record("check_service_status", statuses)
    ctx.answer = "Current service status (simulated monitoring):\n" + "\n".join(
        f"  • {n.upper()}: {s}" for n, s in statuses.items()) + \
        "\n\nIf something isn't working for you personally, describe it and I'll diagnose your account and device."


def ticket_status_answer(ctx: Context) -> None:
    ctx.verbatim = True
    import re
    match = re.search(r"#?\s*(\d+)", ctx.text)
    if match and "ticket" in ctx.text.lower():
        try:
            ticket = get_ticket(int(match[1]), ctx.employee)
        except Exception:
            ctx.answer = f"I can't find ticket #{match[1]} on your account."
            return
        ctx.record("get_ticket_status", {"ticket_id": ticket["id"], "status": ticket["status"]})
        ctx.answer = (f"Ticket #{ticket['id']}: {ticket['title']}\nStatus: {ticket['status'].replace('_', ' ')}. "
                      f"Provisional target: {ticket.get('resolution_estimate', 'pending assessment')}.")
        return
    tickets = [t for t in list_tickets(ctx.employee) if t["status"] != "resolved"][:10]
    ctx.record("list_tickets", {"open": len(tickets)})
    ctx.answer = ("Your open tickets:\n" + "\n".join(f"  • #{t['id']} ({t['status'].replace('_', ' ')}): {t['title']}" for t in tickets)
                  if tickets else "You have no open tickets.")


def devices_answer(ctx: Context) -> None:
    ctx.verbatim = True
    devices = employee_devices(ctx.employee["id"])
    ctx.record("get_employee_devices", {"devices": devices})
    ctx.answer = ("Your managed devices:\n" + "\n".join(
        f"  • {d['id']}: {d['name']}, {d['operating_system']}, {'compliant' if d['compliant'] else 'not compliant'}" for d in devices)
                  if devices else "You have no managed devices on record.")


def how_to_answer(ctx: Context) -> None:
    result = search_knowledge_base(ctx.text)
    ctx.record("search_knowledge_base", {"sources": [r["source"] for r in result["results"]]})
    if not result["results"]:
        ctx.answer = ("I don't have a documented answer for that. If something isn't working, tell me what's "
                      "affected (VPN, email, Wi-Fi, password, software, device) and I'll diagnose and fix it.")
        return
    top = result["results"][0]
    ctx.sources.add(top["source"])
    body = "\n".join(line for line in top["content"].splitlines()
                     if line.strip() and not line.startswith(("Source ID", "Audience", "Environment", "#")))
    ctx.answer = (f"From {top['source']}:\n\n{body[:1200]}\n\nIf something is actually broken, tell me and "
                  "I'll run the checks and fix it for you.")
