"""When IT changes a ticket's status, the employee sees it in the conversation that created the ticket."""
import os

os.environ["AGENT_MODE"] = "pipeline"

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from auth import create_employee
from fastapi import HTTPException
from sessions import create_session, get_session
from tickets import escalate_issue, update_ticket

EMP = {"id": "upd", "name": "Upd Employee", "role": "employee"}
IT = {"id": "upd-it", "name": "IT", "role": "it_support"}


class TicketUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = patch.object(database, "DATABASE_PATH", Path(cls.tmp.name) / "u.db")
        cls.patch.start()
        database.initialize_database()
        create_employee("upd", "Upd Employee", "Test-password-123!")
        create_employee("upd-it", "IT", "Test-password-123!", "it_support")

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.tmp.cleanup()

    def updates(self, session_id):
        return [m for m in get_session(session_id, "upd")["messages"] if m.get("kind") == "ticket_update"]

    def test_status_changes_appear_in_the_employees_conversation(self):
        sid = create_session("upd")
        ticket = escalate_issue("upd", sid, "VPN: licence missing", "No licence.", "other")["ticket"]
        update_ticket(ticket["id"], "in_progress", IT)
        update_ticket(ticket["id"], "resolved", IT)
        texts = [m["content"] for m in self.updates(sid)]
        self.assertEqual(len(texts), 2)
        self.assertIn("working on it", texts[0])
        self.assertIn("marked this resolved", texts[1])
        self.assertTrue(all(f"#{ticket['id']}" in t for t in texts))
        self.assertEqual(len({m["uid"] for m in self.updates(sid)}), 2)

    def test_same_status_does_not_post_again_and_other_sessions_are_untouched(self):
        sid, other = create_session("upd"), create_session("upd")
        ticket = escalate_issue("upd", sid, "Email: licence", "No licence.", "other")["ticket"]
        update_ticket(ticket["id"], "in_progress", IT)
        update_ticket(ticket["id"], "in_progress", IT)
        self.assertEqual(len(self.updates(sid)), 1)
        self.assertEqual(len(self.updates(other)), 0)

    def test_employees_cannot_change_status(self):
        sid = create_session("upd")
        ticket = escalate_issue("upd", sid, "Device: encryption", "Off.", "other")["ticket"]
        with self.assertRaises(HTTPException):
            update_ticket(ticket["id"], "resolved", EMP)
        self.assertEqual(len(self.updates(sid)), 0)


if __name__ == "__main__":
    unittest.main()
