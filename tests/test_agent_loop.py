"""The model-driven loop and its guardrails.

Each test installs a scripted or deliberately misbehaving "model" and checks that the HARNESS still does
the right thing. This proves the guardrails; it does not measure how good a real model is.
"""
import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import llm
from agent import run_agent
from auth import create_employee, redeem_directory_reset
from it_systems import get_state, save_state, set_service_status
from llm import LLMError, LLMReply, ToolCall
from rule_model import RuleModel

EMP = {"id": "loop", "name": "Loop Employee", "role": "employee"}


class Scripted:
    """Plays back a fixed list of replies; records everything it was sent."""
    name = "scripted"

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def chat(self, messages, tools):
        self.sent.append(json.loads(json.dumps(messages)))
        if not self.replies:
            return LLMReply("Done.")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def call(name, **args):
    return LLMReply("", [ToolCall(f"id_{name}", name, args)])


def say(text):
    return LLMReply(text)


class LoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = patch.object(database, "DATABASE_PATH", Path(cls.tmp.name) / "loop.db")
        cls.db_patch.start()
        database.initialize_database()
        create_employee("loop", "Loop Employee", "Test-password-123!")
        create_employee("other", "Other Person", "Test-password-123!")
        with database.connect() as db:
            db.execute("INSERT INTO devices VALUES ('loop-laptop', 'loop', 'Demo laptop', 'Windows 11', 1)")
        cls.env = patch.dict(os.environ, {"AGENT_MODE": "model", "AGENT_PREFETCH": "0"})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        llm.set_llm(None)
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        for who in ("loop", "other"):
            save_state(who, {})
        for service in ("vpn", "email", "network"):
            set_service_status(service, "operational")
        with database.connect() as db:
            db.execute("DELETE FROM ticket_details")
            db.execute("DELETE FROM tickets")

    def fault(self, who="loop", **changes):
        state = get_state(who)
        for path, value in changes.items():
            section, key = path.split("__")
            state[section][key] = value
        save_state(who, state)

    def run_with(self, model, text, session=None):
        llm.set_llm(model)
        return run_agent(text, EMP, session)

    def tickets(self):
        with database.connect() as db:
            return db.execute("SELECT * FROM tickets").fetchall()

    # ---- the happy path: investigate, fix, verify ------------------------------------------------
    def test_diagnose_fix_verify_without_a_ticket(self):
        self.fault(vpn__stale_session=True, vpn__certificate_valid=False)
        model = Scripted(call("run_diagnostics", area="vpn"), call("apply_fix", action="clear_vpn_session"),
                         call("apply_fix", action="renew_vpn_certificate"), say("I fixed the stuck session and renewed your certificate. Please try again."))
        result = self.run_with(model, "vpn keeps dropping me out")
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")
        self.assertEqual(result["routing"], "model")
        self.assertEqual(self.tickets(), [])
        self.assertTrue(get_state("loop")["vpn"]["certificate_valid"])
        verification = [t for t in result["tool_results"] if t["tool"] == "apply_fix"][-1]["result"]["verification"]
        self.assertTrue(verification["all_passed"])

    def test_confirmation_closes_the_issue_but_only_with_passing_checks(self):
        self.fault(vpn__stale_session=True)
        first = self.run_with(RuleModel(), "my vpn drops")
        again = self.run_with(RuleModel(), "yes that fixed it", first["session_id"])
        self.assertEqual(again["outcome"], "resolved")
        self.assertEqual(self.tickets(), [])

    def test_resolution_needs_the_employees_confirmation(self):
        self.fault(vpn__stale_session=True)
        model = Scripted(call("run_diagnostics", area="vpn"), call("apply_fix", action="clear_vpn_session"),
                         call("mark_resolved", area="vpn"), say("All sorted."))
        result = self.run_with(model, "vpn keeps dropping")
        refusal = [t for t in result["tool_results"] if t["tool"] == "mark_resolved"][0]["result"]
        self.assertIn("error", refusal)
        self.assertNotEqual(result["outcome"], "resolved")

    # ---- identity and permissions ------------------------------------------------------------------
    def test_tool_arguments_cannot_change_whose_account_is_touched(self):
        self.fault("other", vpn__stale_session=True)
        self.fault("loop", vpn__stale_session=True)
        model = Scripted(call("run_diagnostics", area="vpn", employee_id="other"),
                         call("apply_fix", action="clear_vpn_session", employee_id="other"), say("ok"))
        self.run_with(model, "vpn drops")
        self.assertFalse(get_state("loop")["vpn"]["stale_session"])
        self.assertTrue(get_state("other")["vpn"]["stale_session"])

    def test_fix_not_listed_by_a_failed_check_is_refused(self):
        model = Scripted(call("apply_fix", action="flush_dns_cache"), say("Done."))
        result = self.run_with(model, "websites are slow")
        self.assertIn("error", result["tool_results"][0]["result"])
        self.assertEqual(get_state("loop")["network"]["dns_cache_stale"], False)

    def test_unknown_tools_and_bad_arguments_are_returned_as_errors(self):
        model = Scripted(call("format_disk"), call("run_diagnostics", area="mainframe"), say("Sorry."))
        result = self.run_with(model, "hello there friend")
        self.assertTrue(all("error" in t["result"] for t in result["tool_results"]))

    # ---- tickets ----------------------------------------------------------------------------------
    def test_ticket_without_diagnostics_is_refused_then_allowed(self):
        self.fault(vpn__license_assigned=False)
        model = Scripted(call("create_ticket", area="vpn", title="VPN broken", reason="idk"),
                         call("run_diagnostics", area="vpn"),
                         call("create_ticket", area="vpn", title="VPN licence missing", reason="No VPN licence is assigned."), say("I've escalated it."))
        result = self.run_with(model, "vpn not connecting")
        self.assertIn("error", result["tool_results"][0]["result"])
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(result["outcome"], "escalated")
        self.assertIn("Ticket #", result["answer"])

    def test_ticket_is_refused_while_an_automatic_fix_is_untried(self):
        self.fault(network__dns_cache_stale=True)
        model = Scripted(call("run_diagnostics", area="network"),
                         call("create_ticket", area="network", title="DNS", reason="DNS broken"),
                         call("apply_fix", action="flush_dns_cache"), say("Flushed the DNS cache."))
        result = self.run_with(model, "internal sites will not load")
        self.assertIn("apply_fix", result["tool_results"][1]["result"]["error"])
        self.assertEqual(self.tickets(), [])
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")

    def test_all_checks_pass_gate_asks_for_more_effort_once(self):
        model = Scripted(call("run_diagnostics", area="email"),
                         call("create_ticket", area="email", title="Outlook", reason="all pass"),
                         call("create_ticket", area="email", title="Outlook", reason="employee confirms the problem remains"), say("Escalated."))
        result = self.run_with(model, "outlook is acting weird")
        self.assertIn("error", result["tool_results"][1]["result"])
        self.assertEqual(len(self.tickets()), 1)

    def test_harness_files_the_ticket_when_the_model_forgets(self):
        self.fault(vpn__license_assigned=False)
        model = Scripted(call("run_diagnostics", area="vpn"), say("I looked at everything; this one should be fixed shortly."))
        result = self.run_with(model, "vpn not connecting")
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(result["outcome"], "escalated")
        self.assertIn("harness_auto_escalation", [t["tool"] for t in result["tool_results"]])

    def test_false_fixed_claim_is_blocked_and_the_problem_is_ticketed(self):
        self.fault(vpn__license_assigned=False)
        model = Scripted(call("run_diagnostics", area="vpn"), say("Everything is fixed, your VPN should now work!"),
                         say("Your VPN licence is missing, which I can't change myself."))
        result = self.run_with(model, "vpn not connecting")
        self.assertNotIn("Everything is fixed", result["answer"])
        self.assertEqual(result["outcome"], "escalated")
        self.assertEqual(len(self.tickets()), 1)

    def test_remaining_failure_after_a_real_fix_is_still_corrected(self):
        self.fault(vpn__certificate_valid=False, vpn__license_assigned=False)
        model = Scripted(call("run_diagnostics", area="vpn"), call("apply_fix", action="renew_vpn_certificate"),
                         say("I renewed the certificate, so everything is fixed and should now work."))
        result = self.run_with(model, "vpn not connecting")
        self.assertIn("Correction", result["answer"])
        self.assertEqual(len(self.tickets()), 1)

    def test_rejected_fix_is_escalated(self):
        self.fault(vpn__certificate_valid=False, blocked_actions=["renew_vpn_certificate"]) if False else None
        state = get_state("loop"); state["vpn"]["certificate_valid"] = False; state["blocked_actions"] = ["renew_vpn_certificate"]; save_state("loop", state)
        model = Scripted(call("run_diagnostics", area="vpn"), call("apply_fix", action="renew_vpn_certificate"), say("I couldn't renew it."))
        result = self.run_with(model, "vpn certificate error")
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(result["outcome"], "escalated")

    def test_declined_tickets_are_respected_and_can_be_sent_later(self):
        self.fault(vpn__license_assigned=False)
        model = Scripted(call("run_diagnostics", area="vpn"), say("Needs a technician."))
        first = self.run_with(model, "vpn not connecting, please don't create a ticket")
        self.assertEqual(self.tickets(), [])
        self.assertEqual(first["outcome"], "escalation_declined")
        later = self.run_with(Scripted(), "ok create a ticket", first["session_id"])
        self.assertEqual(len(self.tickets()), 1)
        self.assertIn("Ticket #", later["answer"])

    # ---- passwords ---------------------------------------------------------------------------------
    def test_reset_link_reaches_the_employee_but_never_the_model_or_the_transcript(self):
        self.fault(directory__password_expired=True)
        model = Scripted(call("run_diagnostics", area="vpn"), call("send_password_reset_link", area="vpn"), say("Your password expired; use the link below."))
        result = self.run_with(model, "vpn says authentication failed")
        self.assertEqual(len(result["reset_links"]), 1)
        self.assertIn("/corporate-password#token=", result["answer"])
        self.assertEqual(result["outcome"], "awaiting_employee_action")
        sent_to_model = json.dumps(model.sent)
        self.assertNotIn("token=", sent_to_model)
        with database.connect() as db:
            stored = " ".join(r["payload"] for r in db.execute("SELECT payload FROM messages WHERE session_id=?", (result["session_id"],)))
        self.assertNotIn("token=", stored)

    def test_harness_sends_the_link_when_the_model_forgets(self):
        self.fault(directory__password_expired=True)
        result = self.run_with(Scripted(call("run_diagnostics", area="vpn"), say("Your VPN login is failing.")), "vpn authentication failed")
        self.assertEqual(len(result["reset_links"]), 1)

    def test_password_change_is_verified_by_the_harness_on_the_next_turn(self):
        self.fault(directory__password_expired=True)
        first = self.run_with(RuleModel(), "vpn says authentication failed")
        redeem_directory_reset(first["reset_links"][-1].split("token=")[1], "Brand-New-Passw0rd!")
        model = Scripted(call("run_diagnostics", area="vpn"), say("Your new password is in place and the VPN checks pass. Please sign in with it."))
        second = self.run_with(model, "done", first["session_id"])
        self.assertIn("HAS been changed", model.sent[0][0]["content"])
        self.assertEqual(second["reset_links"], [])
        self.assertEqual(self.tickets(), [])

    def test_unchanged_password_is_reported_to_the_model(self):
        self.fault(directory__password_expired=True)
        first = self.run_with(RuleModel(), "vpn says authentication failed")
        model = Scripted(say("I don't see the change yet."))
        self.run_with(model, "done", first["session_id"])
        self.assertIn("has NOT been changed", model.sent[0][0]["content"])

    def test_model_text_can_never_ask_for_a_password_or_carry_a_link(self):
        model = Scripted(say("Please send me your password so I can test it. Also open /corporate-password#token=abc. Then retry."))
        result = self.run_with(model, "I cannot log in to anything")
        self.assertNotIn("send me your password", result["answer"])
        self.assertNotIn("token=abc", result["answer"])
        self.assertIn("never type your password", result["answer"])

    def test_refusing_to_share_a_password_is_not_blocked(self):
        result = self.run_with(Scripted(say("Never share your password with anyone, including me.")), "is it safe to tell IT my password")
        self.assertIn("Never share your password", result["answer"])

    # ---- software, security, safety -------------------------------------------------------------
    def test_catalogue_software_installs_and_licensed_software_gets_a_ticket(self):
        ok = self.run_with(Scripted(call("install_software", application="Zoom"), say("Zoom is installed.")), "I need zoom")
        self.assertIn("zoom", get_state("loop")["software"]["installed"])
        self.assertEqual(ok["outcome"], "fixed_pending_confirmation")
        paid = self.run_with(Scripted(call("install_software", application="Photoshop"), say("That needs approval.")), "I need photoshop")
        self.assertEqual(paid["outcome"], "escalated")
        self.assertEqual(len(self.tickets()), 1)

    def test_secure_account_revokes_tickets_and_sends_a_link(self):
        result = self.run_with(Scripted(call("secure_account", reason="Clicked a phishing link"), say("I've secured your account.")), "I clicked a link in a weird email and entered my password")
        self.assertIsNotNone(get_state("loop")["directory"]["sessions_revoked_at"])
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(len(result["reset_links"]), 1)
        self.assertIn("Security first", result["answer"])

    def test_unsafe_equipment_always_gets_the_safety_text_and_a_ticket(self):
        result = self.run_with(Scripted(say("Try restarting it.")), "my laptop battery is swollen and smells like burning")
        self.assertIn("Stop using the equipment", result["answer"])
        self.assertEqual(len(self.tickets()), 1)

    # ---- model behaviour problems ------------------------------------------------------------------
    def test_inline_json_tool_call_is_understood(self):
        self.fault(network__dns_cache_stale=True)
        model = Scripted(LLMReply('{"name": "run_diagnostics", "arguments": {"area": "network"}}'),
                         LLMReply('{"name": "apply_fix", "arguments": {"action": "flush_dns_cache"}}'), say("Flushed."))
        result = self.run_with(model, "websites will not load")
        self.assertTrue(get_state("loop")["network"]["dns_cache_stale"] is False)
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")

    def test_runaway_model_is_stopped_by_the_budget(self):
        model = Scripted(*[call("check_service_status", service=s) for s in ["vpn", "email", "network"] * 6])
        with patch("config.AGENT_MAX_STEPS", 4):
            result = self.run_with(model, "is everything ok")
        self.assertLessEqual(result["model_calls"], 5)
        self.assertTrue(result["answer"])

    def test_empty_final_reply_falls_back_to_the_evidence(self):
        self.fault(network__dns_cache_stale=True)
        result = self.run_with(Scripted(call("run_diagnostics", area="network"), call("apply_fix", action="flush_dns_cache"), say(""), say("")), "websites will not load")
        self.assertIn("flush", result["answer"].lower())

    def test_model_failing_after_work_started_still_escalates_what_it_found(self):
        self.fault(vpn__license_assigned=False)
        model = Scripted(call("run_diagnostics", area="vpn"), LLMError("timeout"))
        result = self.run_with(model, "vpn not connecting")
        self.assertEqual(result["outcome"], "escalated")
        self.assertEqual(len(self.tickets()), 1)

    def test_unreachable_model_falls_back_to_playbooks_in_auto_mode(self):
        self.fault(vpn__stale_session=True)
        with patch.dict(os.environ, {"AGENT_MODE": "auto"}):
            result = self.run_with(Scripted(LLMError("Ollama is not reachable")), "My vpn is not working, fix it.")
        self.assertEqual(result["routing"], "playbook-fallback")
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")
        self.assertIn("not reachable", result["fallback_reason"])

    def test_unreachable_model_in_model_mode_is_honest(self):
        result = self.run_with(Scripted(LLMError("Ollama is not reachable")), "vpn broken")
        self.assertEqual(result["routing"], "offline")
        self.assertIn("offline", result["answer"])
        self.assertEqual(self.tickets(), [])

    # ---- stalled conversations ------------------------------------------------------------------------
    def test_three_messages_without_progress_hand_over_to_a_human(self):
        model = Scripted(say("Which app is affected?"), say("Can you share the exact error text?"), say("Let me think about that."))
        first = self.run_with(model, "something is wrong with my computer")
        second = self.run_with(model, "it just feels off", first["session_id"])
        self.assertEqual(self.tickets(), [])
        third = self.run_with(model, "I don't know what else to say", first["session_id"])
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(third["outcome"], "escalated")
        description = self.tickets()[0]["description"]
        self.assertIn("something is wrong with my computer", description)

    def test_small_talk_never_triggers_the_stalled_handover(self):
        model = Scripted(say("Hi!"), say("Happy to help."), say("You're welcome."))
        first = self.run_with(model, "hello")
        self.run_with(model, "what can you do", first["session_id"])
        third = self.run_with(model, "thanks", first["session_id"])
        self.assertEqual(self.tickets(), [])

    # ---- through the web API ------------------------------------------------------------------------
    def test_chat_endpoint_returns_the_model_trace_and_status_endpoint_reports_mode(self):
        from fastapi.testclient import TestClient
        from main import app, current_employee
        app.dependency_overrides[current_employee] = lambda: EMP
        try:
            llm.set_llm(RuleModel())
            self.fault(network__dns_cache_stale=True)
            client = TestClient(app)
            body = client.post("/chat", json={"message": "internal sites will not load"}).json()
            self.assertEqual(body["routing"], "model")
            self.assertEqual(body["outcome"], "fixed_pending_confirmation")
            self.assertGreaterEqual(body["model_calls"], 2)
            self.assertIn("arguments", body["tool_results"][0])
            with patch.dict(os.environ, {"AGENT_MODE": "playbook"}):
                self.assertIn("playbook", client.get("/agent/status").json()["engine"])
        finally:
            app.dependency_overrides.clear()

    # ---- reasoning leaks, voice, speed ------------------------------------------------------------------
    def test_thinking_text_is_stripped_even_without_an_opening_tag(self):
        from llm import clean_text
        self.assertEqual(clean_text("Okay, let's see. The employee wants X.</think>Your email checks passed."), "Your email checks passed.")
        self.assertEqual(clean_text("<think>plan</think>Hello"), "Hello")
        self.assertEqual(clean_text("<think>half a thought that never ended"), "")

    def test_reply_that_talks_about_the_employee_is_rewritten_to_you(self):
        model = Scripted(say("Leo Novak said his email is not working. The employee should be asked for the error."),
                         say("Could you share the exact error message you see in Outlook?"))
        result = self.run_with(model, "my email is not working")
        self.assertIn("you", result["answer"].lower())
        self.assertNotIn("the employee", result["answer"].lower())

    def test_reply_that_cannot_be_fixed_falls_back_to_the_evidence(self):
        self.fault(network__dns_cache_stale=True)
        model = Scripted(call("run_diagnostics", area="network"), call("apply_fix", action="flush_dns_cache"),
                         say("The employee had stale DNS entries."), say("The employee had stale DNS entries."))
        result = self.run_with(model, "websites will not load")
        self.assertNotIn("The employee had", result["answer"])
        self.assertIn("DNS", result["answer"])

    def test_ticket_text_is_written_for_the_technician_not_to_the_employee(self):
        self.fault(vpn__license_assigned=False)
        model = Scripted(call("run_diagnostics", area="vpn"),
                         call("create_ticket", area="vpn", title="Your VPN is broken", reason="I checked your VPN and you have no licence."), say("Escalated."))
        self.run_with(model, "vpn not connecting")
        ticket = self.tickets()[0]
        text = ticket["title"] + ticket["description"]
        self.assertNotRegex(text.replace("Employee's description", ""), r"\byour VPN\b|\byou have\b")
        self.assertIn("licence", ticket["description"].lower())

    def test_harness_runs_the_obvious_diagnostic_before_the_first_model_step(self):
        self.fault(network__dns_cache_stale=True)
        model = Scripted(call("apply_fix", action="flush_dns_cache"), say("I flushed the DNS cache. Please try again."))
        with patch.dict(os.environ, {"AGENT_PREFETCH": "1"}):
            result = self.run_with(model, "internal sites will not load, my wifi is slow")
        self.assertTrue(result["tool_results"][0].get("prefetched_by_harness"))
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")
        self.assertEqual(result["model_calls"], 2)  # without prefetching it would take 3 slow steps

    def test_progress_is_reported_while_working(self):
        import progress
        self.fault(network__dns_cache_stale=True)
        self.run_with(RuleModel(), "internal sites will not load")
        steps = [s["text"] for s in progress.get("loop")["steps"]]
        self.assertTrue(any("Checked your network" in s for s in steps), steps)
        self.assertFalse(progress.get("loop")["active"])

    # ---- invented fixes ---------------------------------------------------------------------------------
    def test_a_claimed_fix_with_no_action_is_rewritten_honestly(self):
        model = Scripted(say("Your Office 365 is now responding. I fixed the unresolved VPN connection."),
                         say("I haven't changed anything yet. Which app is not responding, and what error do you see?"))
        result = self.run_with(model, "My Office 365 isn't responding. Look into it and fix the problem.")
        self.assertNotIn("I fixed", result["answer"])
        self.assertIn("haven't changed anything", result["answer"])
        self.assertIn("harness_blocked_unperformed_claim", [t["tool"] for t in result["tool_results"]])

    def test_a_claimed_fix_that_stays_false_falls_back_to_a_question(self):
        model = Scripted(say("I fixed it, it is working now."), say("I fixed it, everything is working again."))
        result = self.run_with(model, "something is broken, fix it")
        self.assertNotIn("I fixed", result["answer"])
        self.assertTrue(result["answer"].rstrip().endswith("?"))

    def test_a_real_fix_may_be_claimed(self):
        self.fault(network__dns_cache_stale=True)
        model = Scripted(call("run_diagnostics", area="network"), call("apply_fix", action="flush_dns_cache"),
                         say("I flushed the DNS cache and it should now work. Please try again."))
        result = self.run_with(model, "websites will not load")
        self.assertIn("I flushed", result["answer"])
        self.assertNotIn("harness_blocked_unperformed_claim", [t["tool"] for t in result["tool_results"]])

    def test_office_365_is_treated_as_an_email_problem(self):
        from classifier import keyword_intent
        self.assertEqual(keyword_intent("My Office 360 isn't responding"), "email")
        self.assertEqual(keyword_intent("Office 365 won't open"), "email")

    # ---- outage handling ----------------------------------------------------------------------------
    def test_outage_is_ticketed_as_service_incident_not_a_device_problem(self):
        set_service_status("email", "degraded")
        result = self.run_with(Scripted(call("check_service_status", service="email"), call("run_diagnostics", area="email"), say("There seems to be an email incident.")),
                               "outlook is not receiving mail")
        self.assertEqual(result["outcome"], "escalated")
        self.assertIn("service incident", [t for t in self.tickets()][0]["description"])


if __name__ == "__main__":
    unittest.main()
