"""Regressions from a real demo conversation: mixed 'works but slows down' reports, human requests, greetings."""
import os

os.environ["AGENT_MODE"] = "pipeline"

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import playbooks as pb
import reply_writer
from agent import run_agent
from auth import create_employee

EMP = {"id": "conv", "name": "Maya Patel", "role": "employee"}


class SaysFixedTests(unittest.TestCase):
    def test_real_confirmations(self):
        for text in ("it works now", "yes, working again, thanks", "all good now", "fixed!"):
            self.assertTrue(pb.says_fixed(text), text)

    def test_mixed_or_long_reports_are_not_confirmations(self):
        for text in ("It works fine for sometime after the restart but starts slowing down eventually.",
                     "works after restart but then the problem continues",
                     "it works now but the call still drops sometimes",
                     "works " + "word " * 30):
            self.assertFalse(pb.says_fixed(text), text)


class ClosingTests(unittest.TestCase):
    def test_goodbyes_are_closings_but_problems_are_not(self):
        from classifier import is_closing
        for text in ("No, I'm good, thanks.", "ok bye", "that's all", "thanks a lot"):
            self.assertTrue(is_closing(text), text)
        for text in ("still not working, thanks", "I'm good but it still drops", "my vpn is slow, thanks"):
            self.assertFalse(is_closing(text), text)


class ConversationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = patch.object(database, "DATABASE_PATH", Path(cls.tmp.name) / "c.db")
        cls.patch.start()
        cls.env = patch.dict(os.environ, {"UNDERSTAND_WITH_MODEL": "0"})
        cls.env.start()
        for name in ("REPLY_PROVIDER", "UNDERSTAND_PROVIDER", "LLM_PROVIDER", "REPLY_MODEL"):
            os.environ.pop(name, None)
        database.initialize_database()
        create_employee("conv", "Maya Patel", "Test-password-123!")
        with database.connect() as db:
            db.execute("INSERT INTO devices VALUES ('conv-laptop', 'conv', 'Demo laptop', 'Windows 11', 1)")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.patch.stop()
        cls.tmp.cleanup()

    def test_slowdown_after_restart_is_not_marked_resolved_and_human_request_escalates(self):
        r = run_agent("My laptop is working slow today.", EMP)
        sid = r["session_id"]
        self.assertEqual(r["outcome"] if "outcome" in r else r.get("status"), "needs_clarification")
        r = run_agent("It works fine for sometime after the restart but starts slowing down eventually.", EMP, sid)
        self.assertNotIn("glad it's working", r["answer"])
        self.assertTrue(r["created_tickets"])

    def test_human_request_after_resolution_reopens_the_last_issue(self):
        r = run_agent("My laptop is working slow today.", EMP)
        sid = r["session_id"]
        r = run_agent("yes it works now thanks", EMP, sid)
        self.assertIn("glad", r["answer"])
        r = run_agent("I would like my case to be handled by a human.", EMP, sid)
        self.assertTrue(r["created_tickets"])
        self.assertNotIn("What's the problem", r["answer"])

    def test_long_description_with_a_human_request_goes_straight_to_a_ticket(self):
        r = run_agent("My laptop is very slow and lags a lot. It works fine after a restart but the problem keeps "
                      "coming back. I am not technical so I would like a human to look at it and suggest a solution.", EMP)
        self.assertTrue(r["created_tickets"])
        self.assertNotIn("What's the problem", r["answer"])

    def test_goodbye_after_a_fix_never_creates_a_ticket_and_first_reply_greets(self):
        r = run_agent("Yo, hows it going? My vpn keeps kicking me out. It works on my phone. Fix it asap.", EMP)
        self.assertTrue(r["answer"].startswith("Hi Maya"))
        sid = r["session_id"]
        run_agent("I tried all the steps and now my vpn is working fine.", EMP, sid)
        for bye in ("No, I'm good, thanks.", "ok bye"):
            r = run_agent(bye, EMP, sid)
            self.assertFalse(r["created_tickets"])
            self.assertIn("Glad I could help", r["answer"])

    def test_later_replies_are_not_greeted_again(self):
        seen = {}

        def fake(system, user):
            seen["user"] = user
            return "Hi Maya,\n\nThanks for the details. " + user.split("DRAFT")[1].split("):\n", 1)[1].split("\n\nCOMPANY")[0]

        reply_writer.set_reply_model(fake)
        try:
            with patch.object(reply_writer, "enabled", return_value=True):
                r = run_agent("My laptop is working slow today.", EMP)
                self.assertIn("start with a short greeting", seen["user"])
                run_agent("It is slow with everything.", EMP, r["session_id"])
                self.assertIn("do NOT greet", seen["user"])
        finally:
            reply_writer.set_reply_model(None)


if __name__ == "__main__":
    unittest.main()
