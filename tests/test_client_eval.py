import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx

import client as employee_client
import evaluate


class ClientAndEvaluationTests(unittest.TestCase):
    def test_no_tool_requirement_needs_manual_review(self):
        self.assertTrue(hasattr(evaluate, "assess_result"), "Evaluator needs explicit ungraded status")
        result = {"outcome": "completed", "tool_results": [], "answer": "Please provide your credentials."}
        assessment = evaluate.assess_result({"expected_tool": None}, result)
        self.assertIsNone(assessment["tool_selection_pass"])
        self.assertEqual(assessment["label"], "MANUAL_REVIEW")
        self.assertEqual(assessment["answer_review"], "not_assessed")

    def test_tool_match_does_not_mean_answer_passed(self):
        self.assertTrue(hasattr(evaluate, "assess_result"))
        result = {"outcome": "completed", "tool_results": [{"tool": "prepare_support_ticket"}]}
        assessment = evaluate.assess_result({"expected_tool": "prepare_support_ticket"}, result)
        self.assertEqual(assessment["label"], "TOOL_MATCH")
        self.assertEqual(assessment["answer_review"], "not_assessed")

    def test_missing_tool_is_identified(self):
        self.assertTrue(hasattr(evaluate, "assess_result"))
        result = {"outcome": "completed", "tool_results": []}
        assessment = evaluate.assess_result({"expected_tool": "search_knowledge_base"}, result)
        self.assertFalse(assessment["tool_selection_pass"])
        self.assertEqual(assessment["label"], "TOOL_MISSING")

    def run_client(self, entries, responder):
        transport = httpx.MockTransport(responder)
        connection = httpx.Client(base_url="http://testserver", transport=transport)
        with patch("client.httpx.Client", return_value=connection), patch("builtins.input", side_effect=entries), patch("client.getpass.getpass", return_value="fake-test-password"):
            with redirect_stdout(io.StringIO()):
                employee_client.main()

    def test_first_question_starts_new_session(self):
        requests = []
        def respond(request):
            if request.url.path == "/auth/login":
                return httpx.Response(200, json={"access_token": "test-token"})
            if request.url.path == "/chat":
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"session_id": "test-session", "answer": "Hello", "outcome": "completed", "tool_results": [], "sources": [], "pending_tickets": []})
            return httpx.Response(204)
        self.run_client(["alice", "My VPN fails", "/quit"], respond)
        self.assertEqual(requests, [{"message": "My VPN fails", "session_id": None}])

    def test_bad_resume_does_not_poison_next_request(self):
        requests = []
        def respond(request):
            if request.url.path == "/auth/login":
                return httpx.Response(200, json={"access_token": "test-token"})
            if request.url.path.startswith("/sessions/"):
                return httpx.Response(404, json={"detail": "Session not found."})
            if request.url.path == "/chat":
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"session_id": "test-session", "answer": "Hello", "outcome": "completed", "tool_results": [], "sources": [], "pending_tickets": []})
            return httpx.Response(204)
        self.run_client(["alice", "/resume 11111111-1111-4111-8111-111111111111", "My VPN fails", "/quit"], respond)
        self.assertEqual(requests[0]["session_id"], None)
