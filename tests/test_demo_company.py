"""Demo company + random incident engine: varied, blind, and scored against hidden ground truth."""
import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import collections
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import demo_company
import incidents
from agent import run_agent
from auth import redeem_directory_reset

ROOT = Path(__file__).resolve().parent.parent


@patch("model_client.classify_with_model", return_value="unknown")
class DemoCompanyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DATABASE_PATH", Path(self.tmp.name) / "demo.db")
        self.db_patch.start()
        demo_company.build("Demo-Passw0rd-2026", quiet=True)

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_roster_shape(self, _):
        teams = [e[2] for e in demo_company.EMPLOYEES]
        self.assertEqual((len(teams), teams.count("payments-ops"), teams.count("platform-eng")), (10, 5, 5))

    def test_everyone_starts_healthy(self, _):
        for employee_id in incidents.employees():
            self.assertEqual(incidents.symptoms_for(employee_id), [])

    def test_rounds_vary_per_person(self, _):
        counts = collections.Counter()
        causes = set()
        for seed in range(30):
            created = incidents.roll(seed=seed)
            per_person = collections.Counter(e for e, _, _ in created if e)
            counts.update(per_person.get(e, 0) for e in incidents.employees())
            causes.update(name for _, name, _ in created)
        self.assertGreater(counts[0], 0, "some people must have no problems")
        self.assertGreater(counts[2] + counts[3], 0, "some people must have several")
        self.assertGreater(len(causes), 20, "rounds should draw from most of the catalogue")

    def test_same_seed_same_round(self, _):
        first = [(e, n) for e, n, _ in incidents.roll(seed=7)]
        second = [(e, n) for e, n, _ in incidents.roll(seed=7)]
        self.assertEqual(first, second)

    def test_symptoms_never_reveal_causes(self, _):
        for seed in range(10):
            incidents.roll(seed=seed)
            for employee_id in incidents.employees():
                text = " ".join(incidents.symptoms_for(employee_id)).lower()
                for cause in incidents.CATALOG:
                    self.assertNotIn(cause, text)

    def test_agent_code_never_reads_ground_truth(self, _):
        for module in ["agent.py", "agent_loop.py", "agent_tools.py", "llm.py", "rule_model.py", "playbook_agent.py", "playbooks.py", "it_systems.py", "classifier.py", "tickets.py"]:
            self.assertNotIn("incidents", (ROOT / module).read_text(encoding="utf-8"), module)

    def test_fixed_problem_scores_pass_and_symptom_clears(self, _):
        from it_systems import get_state, save_state
        state = get_state("ben.carter")
        state["vpn"]["stale_session"] = True
        save_state("ben.carter", state)
        with database.connect() as db:
            db.execute("INSERT INTO incidents (roll_id, employee_id, fault, symptoms) VALUES (1, 'ben.carter', 'vpn_stale_session', '[\"VPN drops.\"]')")
        result = run_agent("VPN connects, then drops a few seconds later.", {"id": "ben.carter", "name": "Ben", "role": "employee"})
        self.assertEqual(result["outcome"], "fixed_pending_confirmation")
        self.assertEqual(incidents.symptoms_for("ben.carter"), [])
        rows, extra = incidents.score()
        self.assertTrue(rows[0]["verdict"].startswith("PASS"))
        self.assertEqual(extra, [])

    def test_hidden_problem_needs_ticket(self, _):
        with database.connect() as db:
            db.execute("INSERT INTO incidents (roll_id, employee_id, fault, symptoms) VALUES (1, 'aisha.karim', 'screen_flicker', '[\"x\"]')")
        self.assertTrue(incidents.score()[0][0]["verdict"].startswith("NOT REPORTED"))
        run_agent("My laptop screen flickers when I move the lid", {"id": "aisha.karim", "name": "Aisha", "role": "employee"})
        self.assertTrue(incidents.score()[0][0]["verdict"].startswith("PASS"))

    def test_blind_round_scores_well(self, _):
        names = {e[0]: e[1] for e in demo_company.EMPLOYEES}
        incidents.roll(seed=123)
        for employee_id in incidents.employees():
            employee = {"id": employee_id, "name": names[employee_id], "role": "employee"}
            reported = set()
            while pending := [s for s in incidents.symptoms_for(employee_id) if s not in reported]:
                reported.add(pending[0])
                result = run_agent(pending[0], employee)
                if result["reset_links"]:
                    redeem_directory_reset(result["reset_links"][-1].split("token=")[1], "Another-Passw0rd-1!")
                    run_agent("done", employee, result["session_id"])
        rows, _ = incidents.score()
        graded = [r for r in rows if not r["verdict"].startswith("NOT REPORTED")]
        passed = sum(r["verdict"].startswith("PASS") for r in graded)
        self.assertGreaterEqual(passed / max(len(graded), 1), 0.8, rows)
