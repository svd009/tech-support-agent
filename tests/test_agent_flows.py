"""End-to-end agent behaviour: diagnose -> fix -> verify -> escalate, with simulated faults."""
import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
from agent import run_agent
from auth import create_employee
from classifier import followup_signal, keyword_intent
from it_systems import get_state, save_state, set_service_status
from main import app
from tickets import get_ticket

EMPLOYEE = {"id": "flow", "name": "Flow Employee", "role": "employee"}
SUPPORT = {"id": "flow-support", "name": "Support", "role": "it_support"}


def tools(result):
    return [item["tool"] for item in result["tool_results"]]


@patch("model_client.classify_with_model", return_value="unknown")
class AgentFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = patch.object(database, "DATABASE_PATH", Path(cls.tmp.name) / "flows.db")
        cls.db_patch.start()
        database.initialize_database()
        create_employee("flow", "Flow Employee", "Test-password-123!")
        create_employee("flow-support", "Support", "Test-password-123!", "it_support")
        with database.connect() as db:
            db.execute("INSERT INTO devices VALUES ('flow-laptop', 'flow', 'Demo laptop', 'Windows 11', 1)")
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        save_state("flow", {})
        for service in ("vpn", "email", "network"):
            set_service_status(service, "operational")

    def fault(self, **changes):
        state = get_state("flow")
        for path, value in changes.items():
            section, key = path.split("__")
            state[section][key] = value
        save_state("flow", state)

    def say(self, text, session=None):
        return run_agent(text, EMPLOYEE, session)

    # ---- the reported bug -------------------------------------------------
    def test_reported_example_no_longer_stops_at_service_status(self, _):
        result = self.say("My vpn is not working, fix it.")
        self.assertIn("diagnose_vpn", tools(result))
        self.assertNotIn("follow these steps:", result["answer"])
        # Nothing visibly wrong -> one safe step and one targeted question first, never a dead end.
        self.assertEqual(result["outcome"], "needs_clarification")
        self.assertIn("haven't changed anything", result["answer"])
        self.assertFalse(result["created_tickets"])
        result = self.say("every network, it says connection timed out", result["session_id"])
        self.assertEqual(result["outcome"], "escalated")
        ticket = get_ticket(result["created_tickets"][0]["id"], SUPPORT)
        for expected in ["My vpn is not working, fix it.", "flow-laptop", "VPN certificate", "Why a technician is needed",
                         "every network, it says connection timed out"]:
            self.assertIn(expected, ticket["description"])

    def test_vpn_faults_fixed_and_verified(self, _):
        self.fault(vpn__stale_session=True, vpn__certificate_valid=False, vpn__client_version="5.0.1")
        result = self.say("My VPN is not working")
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")
        for action in ["clear_vpn_session", "renew_vpn_certificate", "update_vpn_client", "verify_vpn"]:
            self.assertIn(action, tools(result))
        self.assertFalse(result["created_tickets"])
        state = get_state("flow")["vpn"]
        self.assertTrue(state["certificate_valid"])
        self.assertFalse(state["stale_session"])
        done = self.say("yes it works now", result["session_id"])
        self.assertEqual(done["outcome"], "resolved")

    def test_still_broken_after_fix_escalates_with_history(self, _):
        self.fault(vpn__stale_session=True)
        first = self.say("VPN keeps disconnecting")
        second = self.say("no, still not working", first["session_id"])
        self.assertEqual(second["outcome"], "escalated")
        ticket = get_ticket(second["created_tickets"][0]["id"], SUPPORT)
        self.assertIn("Terminated the stale VPN gateway session", ticket["description"])
        self.assertIn("still not working", ticket["description"])

    def test_missing_licence_needs_technician(self, _):
        self.fault(vpn__license_assigned=False)
        result = self.say("can't connect to vpn")
        self.assertEqual(result["outcome"], "escalated")
        self.assertEqual(result["created_tickets"][0]["category"], "account_access")

    def test_rejected_fix_is_escalated(self, _):
        self.fault(vpn__certificate_valid=False)
        state = get_state("flow")
        state["blocked_actions"] = ["renew_vpn_certificate"]
        save_state("flow", state)
        result = self.say("VPN certificate error")
        self.assertEqual(result["outcome"], "escalated")
        self.assertIn("rejected", result["answer"])

    def test_service_outage_escalates_without_device_blame(self, _):
        set_service_status("email", "down")
        result = self.say("Outlook is not receiving emails")
        self.assertEqual(result["outcome"], "escalated")
        self.assertIn("not caused by your device", result["answer"])

    def test_mailbox_full_is_archived(self, _):
        self.fault(email__mailbox_used_pct=99)
        result = self.say("I can't send email, mailbox full")
        self.assertIn("archive_old_mail", tools(result))
        self.assertLess(get_state("flow")["email"]["mailbox_used_pct"], 95)

    def test_network_adapter_fixed(self, _):
        self.fault(network__adapter_enabled=False, network__dns_cache_stale=True)
        result = self.say("my wifi is not working")
        self.assertIn("enable_network_adapter", tools(result))
        self.assertIn("flush_dns_cache", tools(result))

    # ---- password ---------------------------------------------------------
    def test_password_reset_link_full_flow(self, _):
        self.fault(directory__locked=True)
        result = self.say("I forgot my password")
        self.assertEqual(result["outcome"], "awaiting_employee_action")
        self.assertIn("unlock_account", tools(result))
        link = result["reset_links"][0]
        self.assertIn(link, result["answer"])
        # The one-time link is shown once but never stored in the transcript.
        with database.connect() as db:
            stored = " ".join(row["payload"] for row in db.execute("SELECT payload FROM messages WHERE session_id=?", (result["session_id"],)))
        self.assertNotIn(link.split("token=")[1], stored)

        early = self.say("done", result["session_id"])
        self.assertIn("don't see a password change", early["answer"])
        token = link.split("token=")[1]
        weak = self.client.post("/auth/corporate-password/reset", json={"token": token, "new_password": "alllowercase1"})
        self.assertEqual(weak.status_code, 400)
        ok = self.client.post("/auth/corporate-password/reset", json={"token": token, "new_password": "Fresh-Passw0rd-1"})
        self.assertEqual(ok.status_code, 200)
        reused = self.client.post("/auth/corporate-password/reset", json={"token": token, "new_password": "Fresh-Passw0rd-2"})
        self.assertEqual(reused.status_code, 400)
        confirmed = self.say("done", result["session_id"])
        self.assertEqual(confirmed["outcome"], "resolved")

    def test_expired_password_blocks_vpn_then_verified(self, _):
        self.fault(directory__password_expired=True)
        result = self.say("VPN says authentication failed")
        self.assertEqual(result["outcome"], "awaiting_employee_action")
        token = result["reset_links"][0].split("token=")[1]
        self.client.post("/auth/corporate-password/reset", json={"token": token, "new_password": "Fresh-Passw0rd-Other-1"})
        after = self.say("done", result["session_id"])
        self.assertEqual(after["outcome"], "fixed_pending_confirmation")
        self.assertIn("diagnose_vpn", tools(after))

    def test_link_problem_reissues_then_escalates(self, _):
        first = self.say("I need to reset my password")
        second = self.say("the link is not working", first["session_id"])
        self.assertTrue(second["reset_links"])
        third = self.say("still not working", second["session_id"])
        self.assertEqual(third["outcome"], "escalated")

    # ---- other categories -------------------------------------------------
    def test_catalogue_software_installed(self, _):
        result = self.say("Please install Zoom")
        self.assertIn("zoom", get_state("flow")["software"]["installed"])
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")

    def test_licensed_software_escalated(self, _):
        result = self.say("I need Photoshop installed")
        self.assertEqual(result["created_tickets"][0]["category"], "admin_install")

    def test_unexpected_mfa_contained_and_escalated(self, _):
        result = self.say("I keep getting MFA prompts I didn't request")
        self.assertIn("revoke_sessions", tools(result))
        self.assertIn("Do NOT approve", result["answer"])
        self.assertEqual(result["created_tickets"][0]["category"], "security_review")

    def test_hardware_and_safety_escalated(self, _):
        self.assertEqual(self.say("my laptop screen is cracked")["outcome"], "escalated")
        smoke = self.say("my laptop charger is smoking")
        self.assertIn("emergency", smoke["answer"])

    def test_no_ticket_request_respected_then_created_on_request(self, _):
        result = self.say("My wifi is not working. Do not create a ticket.")
        self.assertEqual(result["outcome"], "needs_clarification")
        result = self.say("every website, same on my phone hotspot", result["session_id"])
        self.assertEqual(result["outcome"], "escalation_declined")
        self.assertFalse(result["created_tickets"])
        later = self.say("ok create a ticket", result["session_id"])
        self.assertTrue(later["created_tickets"])

    def test_unclear_message_clarifies_once_then_escalates(self, _):
        first = self.say("it does not work")
        self.assertEqual(first["outcome"], "needs_clarification")
        second = self.say("the thing on my desk", first["session_id"])
        self.assertEqual(second["outcome"], "escalated")

    def test_clarification_routes_to_playbook(self, _):
        first = self.say("help")
        second = self.say("it's my VPN", first["session_id"])
        self.assertIn("diagnose_vpn", tools(second))

    def test_same_problem_reuses_ticket(self, _):
        probe = self.say("My VPN is not working")
        first = self.say("still not working, same on every network", probe["session_id"])
        second = self.say("still not working", first["session_id"])
        self.assertEqual(first["created_tickets"][0]["id"], second["created_tickets"][0]["id"])

    # ---- v4 pipeline ------------------------------------------------------
    def test_office_365_mailbox_full_is_fixed_not_just_reported(self, _):
        self.fault(email__mailbox_used_pct=99)
        result = self.say("My Office 365 isn't responding. Look into it and fix the problem.")
        self.assertEqual(result["routing"], "playbook")
        self.assertIn("archive_old_mail", tools(result))
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")
        self.assertIn("99% -> 60%", result["answer"])
        self.assertLess(result["duration_ms"], 5000)
        done = self.say("yes works now", result["session_id"])
        self.assertEqual(done["outcome"], "resolved")
        self.assertFalse(done["created_tickets"])

    def test_clean_checks_probe_then_resolved_without_ticket(self, _):
        first = self.say("vpn keeps dropping every few minutes when im on calls, pls help")
        self.assertEqual(first["outcome"], "needs_clarification")
        self.assertIn("One thing to try", first["answer"])
        self.assertNotRegex(first["answer"].lower(), r"i (fixed|repaired|restarted|reset)")
        done = self.say("reconnecting on the hotspot fixed it, working now", first["session_id"])
        self.assertEqual(done["outcome"], "resolved")
        self.assertFalse(done["created_tickets"])

    def test_probe_is_asked_only_once(self, _):
        first = self.say("outlook is acting weird")
        second = self.say("it's Outlook, it sits on the loading screen forever", first["session_id"])
        self.assertEqual(second["outcome"], "escalated")
        ticket = get_ticket(second["created_tickets"][0]["id"], SUPPORT)
        self.assertIn("Outlook, it sits on the loading screen forever", ticket["description"])
        self.assertIn("suggested one self-service step", ticket["description"])

    def test_probe_answer_naming_an_app_gets_that_app_repaired(self, _):
        first = self.say("my office 365 is acting weird")
        second = self.say("it's Teams, it freezes on the loading screen", first["session_id"])
        self.assertIn("install_software", tools(second))
        self.assertEqual(second["outcome"], "fixed_pending_confirmation")

    def test_excel_problem_asks_then_hands_over_with_details(self, _):
        first = self.say("Excel freezes every time I open the month-end workbook")
        self.assertEqual(first["outcome"], "needs_clarification")
        second = self.say("only that one file, it just hangs on my laptop", first["session_id"])
        self.assertEqual(second["outcome"], "escalated")
        self.assertIn("only that one file", get_ticket(second["created_tickets"][0]["id"], SUPPORT)["description"])

    def test_access_denied_goes_to_a_technician_with_reason(self, _):
        result = self.say("I get access denied opening the team shared drive folder")
        self.assertEqual(result["outcome"], "escalated")
        self.assertIn("haven't changed anything", result["answer"])

    def test_slow_laptop_gets_updates_installed_not_a_hardware_ticket(self, _):
        self.fault(device__patches_current=False)
        result = self.say("my laptop is super slow after the windows update")
        self.assertIn("install_security_updates", tools(result))
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")

    def test_followup_reply_acknowledges_answer_and_does_not_repeat_the_diagnostic(self, _):
        first = self.say("vpn keeps dropping every few minutes when im on calls")
        second = self.say("It happens on home Wi-Fi and on my hotspot, the call freezes", first["session_id"])
        self.assertIn("added your answer to the ticket", second["answer"])
        self.assertNotIn("I ran a full", second["answer"])
        self.assertNotIn("recorded above", second["answer"])
        self.assertNotIn("every fix I attempted", second["answer"])
        self.assertNotIn("did not help", second["answer"])
        description = get_ticket(second["created_tickets"][0]["id"], SUPPORT)["description"]
        self.assertIn("home Wi-Fi and on my hotspot", description)

    def test_noninformative_answer_is_nudged_once_then_passed_on(self, _):
        first = self.say("email isnt working")
        nudge = self.say("idk", first["session_id"])
        self.assertEqual(nudge["outcome"], "needs_clarification")
        self.assertFalse(nudge["created_tickets"])
        final = self.say("idk", first["session_id"])
        self.assertEqual(final["outcome"], "escalated")
        self.assertIn("pass it on as it is", final["answer"])

    def test_still_not_working_after_ticket_updates_it_briefly(self, _):
        first = self.say("vpn not working")
        second = self.say("it fails everywhere", first["session_id"])
        third = self.say("still not working", first["session_id"])
        self.assertEqual(third["created_tickets"][0]["id"], second["created_tickets"][0]["id"])
        self.assertIn("existing ticket", third["answer"])
        self.assertNotIn("Reason:", third["answer"])
        self.assertNotIn("Initial complexity", third["answer"])

    def test_still_not_working_after_a_fix_does_not_repeat_the_diagnostic_opener(self, _):
        self.fault(email__mailbox_used_pct=99)
        first = self.say("outlook shows mailbox full")
        second = self.say("still not working", first["session_id"])
        self.assertIn("sorry it's still not working", second["answer"].lower())
        self.assertNotIn("I ran a full", second["answer"])
        self.assertNotIn("and every check I ran and every fix", second["answer"])

    def test_word_crash_is_understood_and_works_now_closes_a_clarification(self, _):
        first = self.say("word crashes when i open docs")
        self.assertIn("can't see inside desktop apps", first["answer"])
        vague = self.say("its broken")
        done = self.say("restarting fixed it", vague["session_id"])
        self.assertEqual(done["outcome"], "resolved")

    def test_boilerplate_only_mentions_what_really_happened(self, _):
        hardware = self.say("my keyboard keys stopped working")
        self.assertNotIn("every check I ran", hardware["answer"])
        access = self.say("access denied to the finance shared drive")
        self.assertNotIn("every check I ran", access["answer"])
        explicit = self.say("vpn broken")
        asked = self.say("create a ticket", explicit["session_id"])
        self.assertIn("You asked for a technician", asked["answer"])
        for text in (hardware["answer"], access["answer"], asked["answer"]):
            self.assertNotIn("the employee", text.lower())

    def test_declined_then_requested_ticket_never_speaks_ticket_voice_to_the_employee(self, _):
        first = self.say("vpn not working, do not create a ticket")
        second = self.say("it fails everywhere", first["session_id"])
        self.assertNotIn("added your answer to the ticket", second["answer"])
        third = self.say("create a ticket", first["session_id"])
        self.assertNotIn("the employee", third["answer"].lower())

    def test_reply_never_claims_a_fix_that_did_not_run(self, _):
        for text in ["My vpn is not working, fix it.", "email is broken", "wifi not working"]:
            result = self.say(text)
            ran = [t for t in result["tool_results"] if t["result"].get("action")]
            self.assertFalse(ran, text)
            self.assertNotIn("What I found and fixed", result["answer"], text)

    def test_info_intents_do_not_create_tickets(self, _):
        for text in ["hi", "Is the email service down?", "which devices are mine?", "How do I connect to the VPN?"]:
            self.assertFalse(self.say(text)["created_tickets"], text)


class ClassifierTests(unittest.TestCase):
    def test_routes(self):
        cases = {
            "My vpn is not working, fix it.": "vpn",
            "Outlook keeps crashing": "email",
            "I forgot my password": "password",
            "my account is locked": "password",
            "wifi keeps dropping": "network",
            "please install slack": "software",
            "my laptop screen is cracked": "hardware",
            "I got a phishing email": "security",
            "Is the VPN down?": "service_status",
            "laptop says not compliant, bitlocker off": "device_compliance",
        }
        for text, label in cases.items():
            self.assertEqual(keyword_intent(text), label, text)

    def test_followups(self):
        self.assertEqual(followup_signal("yes it works now"), "positive")
        self.assertEqual(followup_signal("no, still not working"), "negative")
        self.assertEqual(followup_signal("nope"), "negative")
        self.assertEqual(followup_signal("done"), "positive")


if __name__ == "__main__":
    unittest.main()
