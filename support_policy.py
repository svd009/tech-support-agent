"""Configured provisional resolution targets per ticket category."""
import json
from pathlib import Path

POLICY = json.loads((Path(__file__).resolve().parent / "support_policy.json").read_text(encoding="utf-8"))


def ticket_receipt(ticket: dict, created: bool = True) -> str:
    action = "Created" if created else "Found your existing open"
    return (
        f"{action} ticket #{ticket['id']}: {ticket['title']}. It is in the local IT support queue.\n"
        f"Initial complexity: {ticket['complexity']}. Provisional resolution target: {ticket['resolution_estimate']}.\n"
        f"{POLICY['notice']}"
    )
