"""Live tool-selection probes. Requires API + Ollama; does not confirm ticket drafts."""
import argparse
import getpass
import json
import time
from pathlib import Path
import httpx


def assess_result(scenario: dict, result: dict) -> dict:
    """Tool matching is a narrow metric. Answer quality remains ungraded."""
    called = [item["tool"] for item in result.get("tool_results", [])]
    expected = scenario["expected_tool"]
    completed = result.get("outcome") not in {None, "tool_error"}
    matched = None if expected is None else completed and expected in called
    if not completed:
        label = "EXECUTION_FAILED"
    elif expected is None:
        label = "MANUAL_REVIEW"
    else:
        label = "TOOL_MATCH" if matched else "TOOL_MISSING"
    return {"tool_selection_pass": matched, "label": label,
            "answer_review": "not_assessed", "called_tools": called}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--review-file", help="Re-label saved results without running the model or modifying the file")
    args = parser.parse_args()
    if args.review_file:
        for saved in json.loads(Path(args.review_file).read_text(encoding="utf-8-sig")):
            assessment = assess_result(saved, saved.get("result", {}))
            print(saved["id"], assessment["label"], assessment["called_tools"], "ANSWER_NOT_ASSESSED")
        return
    scenarios = json.loads((Path(__file__).parent / "eval_scenarios.json").read_text())[:args.limit]
    results = []
    with httpx.Client(base_url=args.url, timeout=300, trust_env=False) as client:
        response = client.post("/auth/login", json={"employee_id": input("Employee ID: "), "password": getpass.getpass("Password: ")})
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        try:
            for scenario in scenarios:
                started = time.monotonic()
                try:
                    response = client.post("/chat", json={"message": scenario["prompt"]})
                    response.raise_for_status()
                    result = response.json()
                    assessment = assess_result(scenario, result)
                    results.append({**scenario, **assessment, "result": result,
                                    "elapsed_seconds": round(time.monotonic() - started, 2)})
                    print(scenario["id"], assessment["label"], assessment["called_tools"], flush=True)
                except httpx.HTTPError as error:
                    results.append({**scenario, "tool_selection_pass": False, "error": type(error).__name__})
                    print(scenario["id"], "ERROR", type(error).__name__, flush=True)
                Path("eval-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        finally:
            client.post("/auth/logout")
    print("Saved eval-results.json. TOOL_MATCH does not establish answer accuracy or safety.")
    print("Review answers using each scenario's manual_check. Drafts created by probes expire after one hour.")


if __name__ == "__main__":
    main()
