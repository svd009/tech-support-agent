"""Blind evaluation: random rounds, a scripted 'employee' who only knows the symptoms, then scoring.

The scripted employee reports each symptom in its own words, confirms fixes ("yes"), and completes
password resets through the real reset page API. It never sees causes. Runs in a throwaway database.

  python blind_eval.py --rounds 20 --seed 1 --mode playbook   # v4 pipeline (code-driven; no model needed)
  python blind_eval.py --rounds 20 --seed 1 --mode stub       # v3 harness + offline stand-in model (plumbing check)
  python blind_eval.py --rounds 2  --seed 1 --mode real       # v3 harness + YOUR model (LLM_PROVIDER / AGENT_MODEL); slow on CPU
"""
import argparse
import collections
import os
import tempfile


import itertools
_PW = itertools.count(1)  # every reset needs a new password: reuse is now rejected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--mode", choices=["real", "stub", "playbook"], default="real")
    parser.add_argument("--people", type=int, default=0, help="only the first N employees each round (real models are slow)")
    args = parser.parse_args()
    os.environ["AGENT_MODE"] = {"real": "model", "stub": "model", "playbook": "playbook"}[args.mode]
    tmp = tempfile.TemporaryDirectory()
    os.environ["SUPPORT_DB"] = os.path.join(tmp.name, "blind.db")
    os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:11434")
    import logging
    logging.disable(logging.CRITICAL)
    import database
    from pathlib import Path
    database.DATABASE_PATH = Path(os.environ["SUPPORT_DB"])
    import demo_company
    import incidents
    from agent import run_agent
    from auth import redeem_directory_reset
    if args.mode == "stub":
        import llm
        from rule_model import RuleModel
        llm.set_llm(RuleModel())
    demo_company.build("Blind-Eval-Passw0rd", quiet=True)
    names = {e[0]: e[1] for e in demo_company.EMPLOYEES}
    totals, by_cause = collections.Counter(), collections.defaultdict(collections.Counter)
    for round_number in range(args.rounds):
        incidents.roll(seed=args.seed + round_number)
        for employee_id in incidents.employees()[: args.people or None]:
            employee = {"id": employee_id, "name": names.get(employee_id, employee_id), "role": "employee"}
            reported = set()
            while True:
                # Re-check before each report: a fix for one problem may have cleared other symptoms.
                pending = [s for s in incidents.symptoms_for(employee_id) if s not in reported]
                if not pending:
                    break
                symptom = pending[0]
                reported.add(symptom)
                result = run_agent(symptom, employee)  # one conversation per symptom, symptom text verbatim
                if result["routing"] == "offline":
                    raise SystemExit(f"The model is not reachable: {result.get('fallback_reason')}\nStart Ollama (ollama pull {os.getenv('AGENT_MODEL') or 'qwen3:4b'}) "
                                     "or set LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY, or use --mode stub / --mode playbook.")
                for _ in range(3):  # play out follow-ups like a cooperative employee
                    if result["reset_links"]:
                        redeem_directory_reset(result["reset_links"][-1].split("token=")[1], f"Blind-Eval-New-Passw0rd-{next(_PW)}!")
                        result = run_agent("done", employee, result["session_id"])
                    elif result["outcome"] == "needs_clarification":
                        result = run_agent("it's on my work laptop", employee, result["session_id"])
                    elif result["outcome"] == "fixed_pending_confirmation":
                        # Honest answer: only say it's fixed if the symptom is actually gone.
                        still = symptom in incidents.symptoms_for(employee_id)
                        result = run_agent("no, still not working" if still else "yes, that fixed it",
                                           employee, result["session_id"])
                    else:
                        break
                if args.verbose:
                    print(f"[{round_number}] {employee_id}: {symptom} -> {result['outcome']}")
        rows, extra = incidents.score()
        for row in rows:
            verdict = row["verdict"].split()[0]
            totals[verdict] += 1
            by_cause[row["cause"]][verdict] += 1
        totals["UNMATCHED_TICKETS"] += len(extra)
    graded = totals["PASS"] + totals["PARTIAL"] + totals["FAIL"]
    print(f"{args.rounds} rounds, {graded} real problems reported.")
    print(f"PASS {totals['PASS']}  PARTIAL {totals['PARTIAL']}  FAIL {totals['FAIL']}  "
          f"=> {100 * totals['PASS'] / max(graded, 1):.0f}% handled correctly; "
          f"{totals['UNMATCHED_TICKETS']} ticket(s) with no matching cause")
    print("\nBy cause (agent can see it? / pass / partial / fail):")
    for cause, counts in sorted(by_cause.items(), key=lambda kv: -sum(kv[1].values())):
        visible = "hidden" if incidents.CATALOG.get(cause, {}).get("kind") == "hidden" else "visible"
        print(f"  {cause:24} {visible:8} pass {counts['PASS']:3}  partial {counts['PARTIAL']:3}  fail {counts['FAIL']:3}")


if __name__ == "__main__":
    main()
