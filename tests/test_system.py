import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
from auth import create_employee
from main import app
from tools import check_service_status
from tickets import prepare_ticket


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path_patch = patch.object(database, "DATABASE_PATH", Path(cls.directory.name) / "test.db")
        cls.path_patch.start()
        database.initialize_database()
        for employee, role in [("alice", "employee"), ("bob", "employee"), ("support", "it_support")]:
            create_employee(employee, employee.title(), "Test-password-123!", role)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.headers = {}
        for employee in ["alice", "bob", "support"]:
            response = cls.client.post("/auth/login", json={"employee_id": employee, "password": "Test-password-123!"})
            cls.headers[employee] = {"Authorization": "Bearer " + response.json()["access_token"]}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.path_patch.stop()
        cls.directory.cleanup()

    def chat(self, message="Check VPN", **extra):
        return self.client.post("/chat", headers=self.headers["alice"], json={"message": message, **extra})

    def test_status_normalization(self):
        self.assertEqual(check_service_status(" VPN ")["status"], "operational")

    def test_unknown_status(self):
        self.assertFalse(check_service_status("printer")["found"])

    def test_blank_status(self):
        with self.assertRaises(ValueError):
            check_service_status(" ")

    def test_wrong_type_status(self):
        with self.assertRaises(TypeError):
            check_service_status(None)

    def test_health(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/tickets").status_code, 401)

    def test_header_is_not_identity(self):
        self.assertEqual(self.client.get("/tickets", headers={"X-Employee-ID": "alice"}).status_code, 401)

    def test_bad_password(self):
        result = self.client.post("/auth/login", json={"employee_id": "alice", "password": "wrong"})
        self.assertEqual(result.status_code, 401)

    def test_logout(self):
        result = self.client.post("/auth/login", json={"employee_id": "bob", "password": "Test-password-123!"})
        headers = {"Authorization": "Bearer " + result.json()["access_token"]}
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/tickets", headers=headers).status_code, 401)

    def test_expired_token(self):
        result = self.client.post("/auth/login", json={"employee_id": "bob", "password": "Test-password-123!"})
        from auth import token_digest
        raw = result.json()["access_token"]
        with database.connect() as db:
            db.execute("UPDATE auth_tokens SET expires_at=0 WHERE token_hash=?", (token_digest(raw),))
        self.assertEqual(self.client.get("/tickets", headers={"Authorization": "Bearer " + raw}).status_code, 401)

    def test_status_api(self):
        result = self.client.get("/services/Vpn/status", headers=self.headers["alice"])
        self.assertEqual(result.json()["service"], "vpn")

    def test_vpn_chat_runs_diagnostics(self):
        result = self.chat("My vpn is not working, fix it.").json()
        tools = [item["tool"] for item in result["tool_results"]]
        self.assertIn("diagnose_vpn", tools)
        self.assertEqual(result["outcome"], "needs_clarification")
        result = self.chat("same on every network, no error shown", session_id=result["session_id"]).json()
        self.assertEqual(result["outcome"], "escalated")
        self.assertTrue(result["created_tickets"])

    def test_ticket_confirmation_flow(self):
        session = self.chat("hi").json()["session_id"]
        draft = prepare_ticket("alice", session, "VPN login", "Authentication failed; internet works.")
        self.assertEqual(self.client.post(f"/ticket-drafts/{draft['id']}/confirm", headers=self.headers["bob"]).status_code, 404)
        first = self.client.post(f"/ticket-drafts/{draft['id']}/confirm", headers=self.headers["alice"])
        second = self.client.post(f"/ticket-drafts/{draft['id']}/confirm", headers=self.headers["alice"])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        ticket_id = first.json()["id"]
        self.assertEqual(self.client.get(f"/tickets/{ticket_id}", headers=self.headers["bob"]).status_code, 404)
        self.assertEqual(self.client.patch(f"/tickets/{ticket_id}", headers=self.headers["alice"], json={"status": "resolved"}).status_code, 403)
        updated = self.client.patch(f"/tickets/{ticket_id}", headers=self.headers["support"], json={"status": "resolved"})
        self.assertEqual(updated.json()["status"], "resolved")

    def test_session_ownership(self):
        session = self.chat("hi").json()["session_id"]
        result = self.client.post("/chat", headers=self.headers["bob"], json={"session_id": session, "message": "Show history"})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.client.get(f"/sessions/{session}", headers=self.headers["bob"]).status_code, 404)

    def test_history_persists(self):
        session = self.chat("My VPN fails").json()["session_id"]
        history = self.client.get(f"/sessions/{session}", headers=self.headers["alice"]).json()
        self.assertEqual(history["messages"][0]["content"], "My VPN fails")

    def test_empty_chat_rejected(self):
        self.assertEqual(self.chat("  ").status_code, 422)

    def test_chat_identity_field_rejected(self):
        self.assertEqual(self.chat(employee_id="bob").status_code, 422)

    def test_initialization_preserves_data(self):
        database.initialize_database()
        with database.connect() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM employees WHERE id='alice'").fetchone())

    def test_expired_draft(self):
        session = self.chat("hi").json()["session_id"]
        draft_id = prepare_ticket("alice", session, "Expired", "Test draft")["id"]
        with database.connect() as db:
            db.execute("UPDATE ticket_drafts SET expires_at=0 WHERE id=?", (draft_id,))
        self.assertEqual(self.client.post(f"/ticket-drafts/{draft_id}/confirm", headers=self.headers["alice"]).status_code, 410)

    def test_no_retrieval_match(self):
        from retrieval import search_knowledge_base
        self.assertEqual(search_knowledge_base("zxqv987654")["results"], [])

    def test_agent_failure_is_contained(self):
        with patch("playbooks.diagnose", side_effect=OSError("test failure")):
            result = self.chat("My VPN is not working").json()
        self.assertEqual(result["outcome"], "tool_error")

    def test_chat_busy(self):
        from main import chat_lock
        chat_lock.acquire()
        try:
            self.assertEqual(self.chat().status_code, 409)
        finally:
            chat_lock.release()


if __name__ == "__main__":
    unittest.main()
