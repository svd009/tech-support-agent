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
from auth import create_employee
from main import app


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = patch.object(database, "DATABASE_PATH", Path(cls.tmp.name) / "web.db")
        cls.db_patch.start()
        database.initialize_database()
        for name in ["web-alice", "web-bob"]:
            create_employee(name, name, "Test-password-123!")
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.headers = {}
        for name in ["web-alice", "web-bob"]:
            token = cls.client.post("/auth/login", json={"employee_id": name, "password": "Test-password-123!"}).json()["access_token"]
            cls.headers[name] = {"Authorization": "Bearer " + token}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def test_web_page_available(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('id="login-form"', response.text)

    def test_static_assets(self):
        for path in ["/static/app.css", "/static/app.js"]:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_session_list_requires_auth(self):
        self.assertEqual(self.client.get("/sessions").status_code, 401)

    def test_session_list_is_owner_scoped(self):
        session_id = self.client.post("/chat", headers=self.headers["web-alice"], json={"message": "My VPN fails"}).json()["session_id"]
        alice = self.client.get("/sessions", headers=self.headers["web-alice"]).json()
        bob = self.client.get("/sessions", headers=self.headers["web-bob"]).json()
        self.assertIn(session_id, [s["id"] for s in alice])
        self.assertNotIn(session_id, [s["id"] for s in bob])
        self.assertEqual(next(s["title"] for s in alice if s["id"] == session_id), "My VPN fails")

    def test_corporate_password_page_available(self):
        self.assertEqual(self.client.get("/corporate-password").status_code, 200)
        self.assertEqual(self.client.get("/static/corporate-password.js").status_code, 200)
