"""v5 reply writer: the model may only reword verified facts; anything else falls back to the built-in reply."""
import os

os.environ["AGENT_MODE"] = "pipeline"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL", "REPLY_PROVIDER", "REPLY_MODEL", "UNDERSTAND_PROVIDER"):
    os.environ.pop(_name, None)

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import reply_writer
from agent import run_agent
from auth import create_employee
from it_systems import save_state, set_service_status

EMPLOYEE = {"id": "rw", "name": "Riley Writer", "role": "employee"}


def draft_of(user: str) -> str:
    return re.search(r"DRAFT \(.*?\):\n(.*?)\n\nCOMPANY KNOWLEDGE", user, re.S).group(1)


class Recorder:
    """Stand-in model: records prompts and answers with whatever the test supplies."""
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise reply_writer.ReplyError(self.error)
        return self.reply(user) if callable(self.reply) else self.reply


def friendly(user):  # a faithful rewrite: same facts, warmer opening
    return "Hi Riley, here is where things stand.\n\n" + draft_of(user)


class ReplyWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = patch.object(database, "DATABASE_PATH", Path(cls.tmp.name) / "rw.db")
        cls.db_patch.start()
        database.initialize_database()
        create_employee("rw", "Riley Writer", "Test-password-123!")
        with database.connect() as db:
            db.execute("INSERT INTO devices VALUES ('rw-laptop', 'rw', 'Demo laptop', 'Windows 11', 1)")

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        save_state("rw", {})
        for service in ("vpn", "email", "network"):
            set_service_status(service, "operational")
        reply_writer.set_reply_model(None)

    def tearDown(self):
        reply_writer.set_reply_model(None)

    def say(self, text, session=None):
        return run_agent(text, EMPLOYEE, session)

    # ---- on / off ------------------------------------------------------------
    def test_templates_only_by_default(self):
        result = self.say("vpn is not working")
        self.assertEqual(result["reply_source"], "template")

    def test_model_reply_used_when_valid_and_prompt_has_facts_draft_and_knowledge(self):
        model = Recorder(friendly)
        reply_writer.set_reply_model(model)
        result = self.say("vpn is not working")
        self.assertEqual(result["reply_source"], "model")
        self.assertTrue(result["answer"].startswith("Hi Riley"))
        system, user = model.calls[0]
        self.assertIn("FACTS", user)
        self.assertIn("nothing_was_changed", user)
        self.assertIn("EMPLOYEE MESSAGE: vpn is not working", user)
        self.assertIn("never claim", system.lower().replace("state an action as done only if", "never claim"))
        self.assertIn("vpn_authentication.md", result["sources"])

    def test_outcome_and_state_are_decided_by_code_not_by_the_model(self):
        reply_writer.set_reply_model(Recorder(friendly))
        first = self.say("vpn keeps dropping")
        self.assertEqual(first["outcome"], "needs_clarification")
        second = self.say("every network", first["session_id"])
        self.assertEqual(second["outcome"], "escalated")
        self.assertTrue(second["created_tickets"])

    # ---- the harness rejects anything unsafe ----------------------------------
    def assertFallsBack(self, reply, text="vpn is not working", needle="rejected"):
        reply_writer.set_reply_model(Recorder(reply))
        result = self.say(text)
        self.assertEqual(result["reply_source"], "template")
        self.assertIn(needle, result["reply_fallback"])
        self.assertIn("haven't changed anything", result["answer"])
        return result

    def test_rejects_a_fabricated_fix(self):
        self.assertFallsBack("Good news, I fixed your VPN and restarted the gateway. Please try connecting again. Is it working?",
                             needle="claims an action")

    def test_rejects_password_requests(self):
        self.assertFallsBack("Please send me your password so I can check the account. What happens when you connect?",
                             needle="password")

    def test_negated_password_advice_is_allowed(self):
        reply_writer.set_reply_model(Recorder(lambda u: draft_of(u) + "\n\nNever share your password with anyone."))
        self.assertEqual(self.say("vpn is not working")["reply_source"], "model")

    def test_rejects_new_links(self):
        self.assertFallsBack("Try this guide: https://example.com/vpn-fix and tell me what you see. Does it help?",
                             needle="link")

    def test_rejects_third_person_and_leaked_reasoning(self):
        self.assertFallsBack("The employee should reconnect and report back to us. Does it work?", needle="employee")
        self.assertFallsBack("<think>plan</think> Short.", needle="rejected")

    def test_rejects_invented_numbers(self):
        self.assertFallsBack(lambda u: draft_of(u) + " This usually takes 45 minutes to clear.", needle="number")

    def test_rejects_a_reply_that_drops_the_question(self):
        self.assertFallsBack("All checks passed and I changed nothing, so the cause is outside what I can see on my side.",
                             needle="question")

    def test_ticket_number_must_be_kept(self):
        reply_writer.set_reply_model(Recorder("I've sent this to the technicians and they will follow up with you soon."))
        first = self.say("vpn keeps dropping")
        reply_writer.set_reply_model(Recorder(lambda u: draft_of(u)))
        second = self.say("only on every network", first["session_id"])
        self.assertEqual(second["reply_source"], "model")
        self.assertRegex(second["answer"], r"#\d+")
        reply_writer.set_reply_model(Recorder("A technician will take it from here and contact you shortly. Thank you for the details."))
        third = self.say("create a ticket", first["session_id"])
        self.assertEqual(third["reply_source"], "template")
        self.assertIn("ticket number", third["reply_fallback"])

    # ---- reliability ---------------------------------------------------------
    def test_model_errors_fall_back_and_trip_a_short_circuit_breaker(self):
        model = Recorder(error="ConnectError: unreachable")
        reply_writer.set_reply_model(model)
        for _ in range(2):
            result = self.say("vpn is not working")
            self.assertEqual(result["reply_source"], "template")
            self.assertIn("unreachable", result["reply_fallback"])
        before = len(model.calls)
        third = self.say("vpn is not working")
        self.assertEqual(len(model.calls), before)  # no more waiting on a dead model
        self.assertIn("recently unreachable", third["reply_fallback"])
        self.assertIn("haven't changed anything", third["answer"])

    def test_reset_links_and_safety_text_never_go_through_the_model(self):
        model = Recorder(friendly)
        reply_writer.set_reply_model(model)
        link = self.say("i forgot my password")
        self.assertEqual(link["reply_source"], "template")
        self.assertIn("/corporate-password#token=", link["answer"])
        safety = self.say("my laptop battery is swollen")
        self.assertEqual(safety["reply_source"], "template")
        self.assertEqual(model.calls, [])

    def test_stored_history_keeps_the_sent_reply_without_links(self):
        reply_writer.set_reply_model(Recorder(friendly))
        result = self.say("vpn is not working")
        import json
        with database.connect() as db:
            stored = [json.loads(r["payload"]) for r in db.execute(
                "SELECT payload FROM messages WHERE session_id=?", (result["session_id"],))]
        assistant = [m["content"] for m in stored if m["role"] == "assistant"]
        self.assertTrue(any(c.startswith("Hi Riley") for c in assistant))


class ValidatorTests(unittest.TestCase):
    def test_validator_accepts_the_draft_itself(self):
        draft = ("I checked VPN and all 12 checks pass. I haven't changed anything. Does it happen on every network?\n"
                 "Ticket #7 is open. Never type your password into this chat.")
        self.assertIsNone(reply_writer.validate(draft, draft, {"checks_run": 12}))

    def test_provider_name_is_validated(self):
        with patch.dict(os.environ, {"REPLY_PROVIDER": "mystery"}):
            with self.assertRaises(reply_writer.ReplyError):
                reply_writer.complete("s", "u")

    def test_bedrock_requires_a_model_id(self):
        with patch.dict(os.environ, {"REPLY_PROVIDER": "bedrock"}, clear=False):
            os.environ.pop("REPLY_MODEL", None)
            with self.assertRaises(reply_writer.ReplyError) as raised:
                reply_writer.complete("s", "u")
            self.assertIn("REPLY_MODEL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
