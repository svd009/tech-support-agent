r"""Check that your hosted/local model can write replies: credentials, model id, speed and the harness checks.

PowerShell example (Amazon Bedrock):
  $env:REPLY_PROVIDER = "bedrock"
  $env:REPLY_MODEL    = "<model id copied from the Bedrock console>"
  $env:AWS_REGION     = "us-east-1"
  .\.venv\Scripts\python.exe check_reply_model.py
  .\.venv\Scripts\python.exe check_reply_model.py --list     # list model ids your account can see (Bedrock)

It makes three real calls (about a cent or less in total) and prints, for each: seconds taken, whether the harness
would send the model's reply or fall back to the built-in one, and the reply itself. Run it once per candidate model
(for example Haiku and Sonnet) and keep the one that answers in a few seconds.
"""
import argparse
import os
import sys
import time

import reply_writer

SAMPLES = [
    {"message": "vpn keeps dropping every few minutes when im on calls, pls help",
     "draft": ("I ran a full VPN diagnostic on your account, your device and the service. All 12 VPN checks pass. "
               "I haven't changed anything, because nothing I can check is broken, so the cause is something my checks can't see.\n\n"
               "One thing to try: disconnect, fully quit the VPN app and reconnect, ideally once on a different network such as a phone hotspot.\n\n"
               "To pin it down: Does it happen on every network or only one (home Wi-Fi, office, hotspot), and what exact message do you see?"),
     "facts": {"outcome": "needs_clarification", "checks_run": 12, "checks_passing": 12, "checks_failing": [],
               "actions_done_this_turn": [], "actions_rejected_this_turn": [], "tickets": [],
               "nothing_was_changed": True, "waiting_for_employee": True}},
    {"message": "My Office 365 isn't responding. Look into it and fix the problem.",
     "draft": ("What I found and fixed:\n  • Moved mail older than 12 months to the online archive (99% -> 60%)\n\n"
               "I re-checked everything: All 7 Email checks pass.\n\n"
               'Please close and reopen Outlook (allow a minute for it to sync). Does it work now? Reply "yes" or "still not working".'),
     "facts": {"outcome": "fixed_pending_confirmation", "checks_run": 7, "checks_passing": 7, "checks_failing": [],
               "actions_done_this_turn": ["Moved mail older than 12 months to the online archive (99% -> 60%)"],
               "actions_rejected_this_turn": [], "tickets": [], "nothing_was_changed": False, "waiting_for_employee": True}},
    {"message": "it happens on home wifi and my hotspot, the call just freezes",
     "draft": ("Thanks, I've added your answer to the ticket. My checks still all pass, so this needs a technician to look at it directly.\n"
               "I couldn't fix this automatically, so I've raised ticket #12 for the Tech Support team.\n"
               "Reason: Everything I can check on the VPN side passes, so the cause is outside what I can inspect or change.\n\n"
               "The ticket already includes your description, your device and every check I ran, so a technician won't need to ask you to repeat anything.\n"
               "Initial complexity: standard. Provisional target: 1-2 business days (a technician confirms timing)."),
     "facts": {"outcome": "escalated", "checks_run": 12, "checks_passing": 12, "checks_failing": [],
               "actions_done_this_turn": [], "actions_rejected_this_turn": [],
               "tickets": [{"id": 12, "complexity": "standard", "target": "1-2 business days"}],
               "nothing_was_changed": True, "waiting_for_employee": False}},
]


class FakeCtx:
    def __init__(self, sample):
        self.text, self.answer = sample["message"], sample["draft"]
        self.employee, self.state = {"id": "check", "name": "Sample Employee"}, {"issues": {}}


def list_models():
    import boto3
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    client = boto3.client("bedrock", region_name=region)
    print(f"Models visible in {region} (copy the id you want into REPLY_MODEL):")
    for model in client.list_foundation_models().get("modelSummaries", []):
        if "TEXT" in model.get("outputModalities", []):
            print(f"  {model['modelId']:62} {model.get('providerName', '')}")
    try:
        for profile in client.list_inference_profiles().get("inferenceProfileSummaries", []):
            print(f"  {profile['inferenceProfileId']:62} (inference profile)")
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list Bedrock model ids visible to your account")
    args = parser.parse_args()
    kind = reply_writer.provider()
    print(f"provider={kind}  model={reply_writer.model_id() or '(default)'}  timeout={reply_writer.timeout()}s")
    if args.list:
        list_models()
        return
    if kind == "none":
        sys.exit("REPLY_PROVIDER is not set. Use bedrock, anthropic or ollama (see the top of this file).")
    sent = 0
    for number, sample in enumerate(SAMPLES, 1):
        ctx = FakeCtx(sample)
        started = time.monotonic()
        try:
            prompt = reply_writer.build_prompt(ctx, sample["facts"], "")
            raw = reply_writer.clean_text(reply_writer.complete(reply_writer.SYSTEM, prompt)).strip()
        except Exception as error:
            print(f"\n[{number}] FAILED after {time.monotonic() - started:.1f}s: {error}")
            print("    Credentials, region, model id and model access are the usual causes. Try --list (Bedrock).")
            continue
        seconds = time.monotonic() - started
        reason = reply_writer.validate(raw, sample["draft"], sample["facts"])
        sent += reason is None
        print(f"\n[{number}] {seconds:.1f}s  {'WOULD BE SENT' if reason is None else 'WOULD BE REJECTED: ' + reason}")
        print("    " + raw.replace("\n", "\n    "))
    print(f"\n{sent} of {len(SAMPLES)} replies would be sent. Speed target: a few seconds per reply.")


if __name__ == "__main__":
    main()