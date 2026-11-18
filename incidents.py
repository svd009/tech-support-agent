"""Random incident engine for blind testing of the support agent.

Each round ("roll") randomly breaks things across the company:
  * most employees get 0-1 problems, some get 2-3, and the odds depend on who they are
    (remote staff hit more VPN trouble, a new hire is missing licences, only office staff use printers)
  * one root cause can show up in several places (an expired password breaks VPN AND email)
  * many root causes share the same symptom ("VPN says authentication failed" has 4 possible causes)
  * some causes are HIDDEN: the agent's diagnostics cannot see them, so the right move is a ticket
  * occasionally a whole service has an outage

You (the tester) only get the symptoms, in the employee's words. The agent only gets what its
diagnostics observe. Ground truth lives in the `incidents` table, which the agent code never reads.
After chatting, `score` compares what the agent did with what was actually wrong.

  python incidents.py roll [--seed 42]     new random round (all systems reset first)
  python incidents.py roll --add           add another wave on top (problems accumulate)
  python incidents.py symptoms [employee]  what each employee is experiencing (no causes)
  python incidents.py score                reveal causes and grade the agent
"""
import argparse
import json
import random
import time
from datetime import date

from database import connect, initialize_database
from it_systems import DEFAULT_STATE, MAILBOX_LIMIT_PCT, REQUIRED_VPN_CLIENT, _version, get_state, save_state, set_service_status

# kind: observable (agent diagnostics can see it) | hidden (they can't) | request (employee needs something)
# expected: agent (agent should fix it) | employee (agent should hand over a reset link) | technician (ticket)
# effect: (section, key, broken value) applied to the simulated systems; None for hidden faults
# tickets: ticket categories that count as a correct escalation
F = dict
CATALOG = {
    # ---- VPN
    "vpn_stale_session": F(kind="observable", expected="agent", weight=6, effect=("vpn", "stale_session", True),
        symptoms={"vpn": ["VPN connects, then drops a few seconds later.", "VPN says you're already signed in from another session.", "VPN spins on 'Connecting...' and never finishes."]}),
    "vpn_cert_expired": F(kind="observable", expected="agent", weight=4, effect=("vpn", "certificate_valid", False),
        symptoms={"vpn": ["VPN shows 'Authentication failed'.", "VPN shows a certificate warning and won't connect.", "VPN spins on 'Connecting...' and never finishes."]}),
    "vpn_client_outdated": F(kind="observable", expected="agent", weight=4, effect=("vpn", "client_version", "5.1.2"),
        symptoms={"vpn": ["The VPN app shows an 'update required' banner.", "VPN spins on 'Connecting...' and never finishes."]}),
    "vpn_profile_missing": F(kind="observable", expected="agent", weight=2, effect=("vpn", "profile_ok", False),
        symptoms={"vpn": ["The VPN app has no 'Corporate' connection to pick anymore.", "The VPN app says the connection profile is missing."]}),
    "vpn_no_license": F(kind="observable", expected="technician", weight=0.5, effect=("vpn", "license_assigned", False),
        tickets={"account_access", "vpn_connectivity"},
        symptoms={"vpn": ["VPN shows 'Authentication failed'.", "VPN says 'User not authorized for remote access'."]}),
    # ---- account
    "password_expired": F(kind="observable", expected="employee", weight=5, effect=("directory", "password_expired", True),
        symptoms={"vpn": ["VPN shows 'Authentication failed'."], "email": ["Outlook keeps popping up asking for your password."],
                  "login": ["Your laptop sign-in says your password has expired."]}),
    "account_locked": F(kind="observable", expected="employee", weight=3, effect=("directory", "locked", True),
        symptoms={"login": ["Sign-in fails everywhere with 'account locked'.", "You can't sign in to your laptop or email."],
                  "vpn": ["VPN shows 'Authentication failed'."]}),
    # ---- email
    "mailbox_full": F(kind="observable", expected="agent", weight=4, effect=("email", "mailbox_used_pct", 99),
        symptoms={"email": ["Emails you send bounce back.", "Outlook shows a red 'mailbox full' warning."]}),
    "mail_profile_corrupt": F(kind="observable", expected="agent", weight=2, effect=("email", "profile_ok", False),
        symptoms={"email": ["Outlook crashes a few seconds after opening.", "Outlook is stuck on 'Loading profile...'."]}),
    "mail_sync_stuck": F(kind="observable", expected="agent", weight=4, effect=("email", "sync_stuck", True),
        symptoms={"email": ["No new email has arrived since this morning.", "Outlook says 'Disconnected' at the bottom."]}),
    "mail_no_license": F(kind="observable", expected="technician", weight=0.3, effect=("email", "license_assigned", False),
        tickets={"account_access", "email_service"},
        symptoms={"email": ["Outlook says it can't find your mailbox.", "You can't sign in to email at all."]}),
    # ---- network
    "wifi_adapter_disabled": F(kind="observable", expected="agent", weight=2, effect=("network", "adapter_enabled", False),
        symptoms={"network": ["Your laptop shows no Wi-Fi networks at all.", "No internet on the laptop, but your phone works fine."]}),
    "dhcp_lease_expired": F(kind="observable", expected="agent", weight=3, effect=("network", "dhcp_lease_valid", False),
        symptoms={"network": ["Wi-Fi says 'Connected, no internet'.", "No internet on the laptop, but your phone works fine."]}),
    "dns_stale": F(kind="observable", expected="agent", weight=3, effect=("network", "dns_cache_stale", True),
        symptoms={"network": ["Some websites load but internal company sites don't.", "Browsers say 'server not found' even though Teams works."]}),
    "nic_hardware_fault": F(kind="observable", expected="technician", weight=0.7, effect=("network", "nic_hardware_fault", True),
        tickets={"hardware_repair", "network_incident"},
        symptoms={"network": ["Wi-Fi keeps dropping every few minutes.", "No internet on the laptop, but your phone works fine."]}),
    # ---- device compliance
    "encryption_off": F(kind="observable", expected="agent", weight=2, effect=("device", "disk_encryption", False),
        symptoms={"device": ["A pop-up says your device is not compliant.", "Company apps block you with a compliance error."]}),
    "patches_missing": F(kind="observable", expected="agent", weight=4, effect=("device", "patches_current", False),
        symptoms={"device": ["A pop-up says your device is not compliant.", "The laptop keeps nagging about pending security updates."]}),
    "antivirus_stopped": F(kind="observable", expected="agent", weight=2, effect=("device", "antivirus_running", False),
        symptoms={"device": ["A security warning says virus protection is turned off."]}),
    "antivirus_tampered": F(kind="observable", expected="technician", weight=0.3, effect=("device", "antivirus_tampered", True),
        tickets={"security_review"},
        symptoms={"device": ["A security warning says virus protection is turned off.", "A pop-up says your device is not compliant."]}),
    # ---- requests
    "needs_zoom": F(kind="request", expected="agent", weight=3, effect=None, package="zoom",
        symptoms={"software": ["You need Zoom for a client call this afternoon, and it isn't installed."]}),
    "needs_slack": F(kind="request", expected="agent", weight=2, effect=None, package="slack",
        symptoms={"software": ["A partner team wants you on Slack, which isn't on your laptop."]}),
    "needs_tableau": F(kind="request", expected="technician", weight=1, effect=None, tickets={"admin_install"},
        symptoms={"software": ["You need Tableau Desktop for a new dashboard project."]}),
    # ---- hidden: the agent has no check that can see these
    "outlook_addin_crash": F(kind="hidden", expected="technician", weight=2, tickets={"email_service", "other"},
        symptoms={"email": ["Outlook crashes a few seconds after opening."]}),
    "vpn_gateway_routing": F(kind="hidden", expected="technician", weight=1, tickets={"vpn_connectivity", "network_incident"},
        symptoms={"vpn": ["VPN connects, but internal sites still won't load."]}),
    "excel_file_corrupt": F(kind="hidden", expected="technician", weight=2, tickets={"other", "admin_install"},
        symptoms={"software": ["Excel freezes every time you open the month-end reconciliation workbook."]}),
    "shared_drive_access": F(kind="hidden", expected="technician", weight=2, tickets={"account_access", "other"},
        symptoms={"access": ["You get 'Access denied' opening the team's shared drive folder."]}),
    "disk_nearly_full": F(kind="hidden", expected="technician", weight=2, tickets={"hardware_repair", "other", "device_compliance"},
        symptoms={"device": ["Your laptop has become extremely slow over the last week."]}),
    "screen_flicker": F(kind="hidden", expected="technician", weight=1, tickets={"hardware_repair"},
        symptoms={"hardware": ["Your laptop screen flickers, worse when you move the lid."]}),
    "keyboard_keys_dead": F(kind="hidden", expected="technician", weight=1, tickets={"hardware_repair"},
        symptoms={"hardware": ["Several keys on your laptop keyboard stopped working."]}),
    "battery_swelling": F(kind="hidden", expected="technician", weight=0.3, tickets={"hardware_repair"},
        symptoms={"hardware": ["The trackpad is lifting and the laptop case looks bulged."]}),
    "printer_jam": F(kind="hidden", expected="technician", weight=1.5, office_only=True, tickets={"hardware_repair", "other"},
        symptoms={"hardware": ["The office printer says 'paper jam' but there's no paper stuck."]}),
    "phishing_received": F(kind="hidden", expected="technician", weight=1.5, tickets={"security_review"},
        symptoms={"security": ["You got an email from 'IT Support' asking you to confirm your password through a link."]}),
    "mfa_fatigue": F(kind="hidden", expected="technician", weight=0.5, tickets={"security_review"},
        symptoms={"security": ["Your phone keeps buzzing with sign-in approval requests you didn't start."]}),
}
OUTAGES = {"vpn": "Lots of colleagues are saying VPN won't connect today.",
           "email": "Email for everyone is arriving very late or not at all."}
PROBLEM_COUNT = [0, 0, 0, 0, 1, 1, 1, 1, 2, 3]  # 40% none, 40% one, 10% two, 10% three


def _profile(employee_id):
    with connect() as db:
        row = db.execute("SELECT * FROM employee_profiles WHERE employee_id=?", (employee_id,)).fetchone()
        device = db.execute("SELECT operating_system FROM devices WHERE employee_id=?", (employee_id,)).fetchone()
    profile = dict(row) if row else {"location": "", "start_date": "2000-01-01"}
    profile["os"] = device["operating_system"] if device else ""
    profile["remote"] = "Remote" in profile["location"]
    profile["office"] = "office" in profile["location"].lower() or "Hybrid" in profile["location"]
    profile["new_hire"] = (date.today() - date.fromisoformat(profile["start_date"])).days <= 14
    return profile


def _weight(name, fault, profile):
    weight = fault["weight"]
    if fault.get("office_only") and not profile["office"]:
        return 0
    if name.startswith("vpn_") and profile["remote"]:
        weight *= 2.5
    if name.startswith("vpn_") and profile["location"].endswith("office"):
        weight *= 0.4  # office staff rarely need VPN
    if name in {"vpn_no_license", "mail_no_license", "needs_zoom", "needs_slack"} and profile["new_hire"]:
        weight *= 25
    return weight


def employees():
    with connect() as db:
        return [r["id"] for r in db.execute("SELECT id FROM employees WHERE role='employee' ORDER BY id")]


def roll(seed=None, add=False, outage_chance=0.1):
    initialize_database()
    rng = random.Random(seed)
    roll_id = int(time.time())
    if not add:
        with connect() as db:
            db.execute("UPDATE incidents SET active=0")
        for service in ("vpn", "email", "network"):
            set_service_status(service, "operational")
    created = []
    for employee_id in employees():
        profile = _profile(employee_id)
        state = get_state(employee_id) if add else json.loads(json.dumps(DEFAULT_STATE))
        if not add:
            state["software"]["installed"] = ["chrome", "teams"]
        pool = {n: _weight(n, f, profile) for n, f in CATALOG.items()}
        for _ in range(rng.choice(PROBLEM_COUNT)):
            names = [n for n, w in pool.items() if w > 0]
            if not names:
                break
            name = rng.choices(names, weights=[pool[n] for n in names])[0]
            pool[name] = 0  # no duplicates for the same person
            fault = CATALOG[name]
            if fault.get("effect"):
                section, key, value = fault["effect"]
                state[section][key] = value
            if fault.get("package") in state["software"]["installed"]:
                state["software"]["installed"].remove(fault["package"])
            symptoms = [rng.choice(options) for options in fault["symptoms"].values()]
            created.append((employee_id, name, symptoms))
        save_state(employee_id, state)
    if rng.random() < outage_chance:
        service = rng.choice(list(OUTAGES))
        set_service_status(service, rng.choice(["degraded", "down"]))
        created.append((None, f"outage_{service}", [OUTAGES[service]]))
    with connect() as db:
        ticket_floor = db.execute("SELECT COALESCE(MAX(id), 0) FROM tickets").fetchone()[0]
        message_floor = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        if add:  # a later wave keeps the round's original floors
            prior = db.execute("SELECT MIN(ticket_floor), MIN(message_floor) FROM incidents WHERE active=1").fetchone()
            if prior[0] is not None:
                ticket_floor, message_floor = prior
        db.executemany("""INSERT INTO incidents (roll_id, employee_id, fault, symptoms, ticket_floor, message_floor)
            VALUES (?, ?, ?, ?, ?, ?)""", [(roll_id, e, n, json.dumps(s), ticket_floor, message_floor) for e, n, s in created])
    return created


def active_incidents(employee_id=None):
    with connect() as db:
        rows = db.execute("SELECT * FROM incidents WHERE active=1 ORDER BY id").fetchall()
    return [dict(r) for r in rows if employee_id is None or r["employee_id"] in (employee_id, None)]


def symptoms_for(employee_id) -> list[str]:
    """What the employee notices. Never includes the cause."""
    seen = []
    state = get_state(employee_id)
    for incident in active_incidents(employee_id):
        if incident["employee_id"] and not _still_broken(incident, state):
            continue  # already fixed: the employee no longer notices it
        if incident["employee_id"] is None and _outage_over(incident):
            continue
        for line in json.loads(incident["symptoms"]):
            if line not in seen:
                seen.append(line)
    return seen


def _outage_over(incident):
    from it_systems import service_status
    return service_status(incident["fault"].removeprefix("outage_")) == "operational"


def _still_broken(incident, state):
    name = incident["fault"]
    fault = CATALOG.get(name)
    if name.startswith("outage_") or fault["kind"] == "hidden":
        return True  # only a technician can clear these
    if fault["kind"] == "request":
        return fault.get("package") not in state["software"]["installed"] if fault.get("package") else True
    section, key, broken = fault["effect"]
    current = state[section][key]
    if key == "client_version":
        return _version(current) < _version(REQUIRED_VPN_CLIENT)
    if key == "mailbox_used_pct":
        return current >= MAILBOX_LIMIT_PCT
    return current == broken


def score():
    rows, matched = [], set()
    incidents = active_incidents()
    if not incidents:
        return rows, []
    ticket_floor = min(i["ticket_floor"] for i in incidents)
    message_floor = min(i["message_floor"] for i in incidents)
    with connect() as db:
        tickets = [dict(r) for r in db.execute("""SELECT t.id, t.employee_id, t.title, d.category FROM tickets t
            LEFT JOIN ticket_details d ON d.ticket_id=t.id WHERE t.id>?""", (ticket_floor,))]
        chatted = {r["employee_id"] for r in db.execute("""SELECT DISTINCT s.employee_id FROM messages m
            JOIN sessions s ON s.id=m.session_id WHERE m.id>?""", (message_floor,))}
    for incident in incidents:
        name, employee_id = incident["fault"], incident["employee_id"]
        if name.startswith("outage_"):
            fault = {"expected": "technician", "tickets": {"service_outage", "vpn_connectivity", "email_service"}}
            owners = None
        else:
            fault = CATALOG[name]
            owners = {employee_id}
        broken = _still_broken(incident, get_state(employee_id)) if employee_id else True
        hits = [t for t in tickets if (owners is None or t["employee_id"] in owners)
                and t["category"] in fault.get("tickets", set())]
        matched.update(t["id"] for t in hits)
        reported = owners is None or employee_id in chatted
        if not reported and broken:
            verdict = "NOT REPORTED (no chat yet)"
        elif fault["expected"] in {"agent", "employee"}:
            verdict = "PASS fixed" if not broken else ("PARTIAL escalated, but the agent could have fixed it" if hits else "FAIL not fixed")
        else:
            verdict = f"PASS ticket #{hits[0]['id']}" if hits else "FAIL needed a technician ticket"
        rows.append({"employee": employee_id or "(everyone)", "cause": name,
                     "visible_to_agent": "yes" if CATALOG.get(name, {}).get("kind") != "hidden" else "no",
                     "should": fault["expected"], "verdict": verdict})
    extra = [t for t in tickets if t["id"] not in matched]
    return rows, extra


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("roll")
    r.add_argument("--seed", type=int)
    r.add_argument("--add", action="store_true")
    s = sub.add_parser("symptoms")
    s.add_argument("employee", nargs="?")
    sub.add_parser("score")
    args = parser.parse_args()
    initialize_database()
    if args.command == "roll":
        created = roll(args.seed, args.add)
        affected = {e for e, _, _ in created if e}
        print(f"New round: {len(created)} problem(s) across {len(affected)} of {len(employees())} employees. "
              "Causes are hidden; run 'symptoms' to see what people are experiencing.")
    elif args.command == "symptoms":
        for employee_id in ([args.employee] if args.employee else employees()):
            lines = symptoms_for(employee_id)
            print(f"\n{employee_id}: " + ("nothing wrong today" if not lines else ""))
            for line in lines:
                print(f"  - {line}")
    else:
        rows, extra = score()
        if not rows:
            print("No active round. Run 'roll' first.")
            return
        width = max(len(r["cause"]) for r in rows)
        for row in rows:
            print(f"{row['employee']:15} {row['cause']:{width}}  agent can see: {row['visible_to_agent']:3}  "
                  f"should: {row['should']:10} -> {row['verdict']}")
        graded = [r for r in rows if not r["verdict"].startswith("NOT REPORTED")]
        passed = sum(r["verdict"].startswith("PASS") for r in graded)
        print(f"\nScore: {passed}/{len(graded)} reported problems handled correctly.")
        for t in extra:
            print(f"Ticket #{t['id']} ({t['employee_id']}, {t['category']}) matches no real cause: '{t['title']}'")


if __name__ == "__main__":
    main()
