"""Offline STAND-IN for a language model, used to exercise the harness without Ollama or an API key.

It is not intelligent: it maps keywords to tools and writes plain sentences from tool results. Its only
purpose is to prove that the plumbing (tool dispatch, verification, guardrails, tickets, reset links,
state across turns, scoring) works end to end. Pass rates measured with it say NOTHING about how well a
real model understands free-form requests. For that, run the same blind evaluation with a real model:

    python blind_eval.py --rounds 3          (uses AGENT_MODEL / LLM_PROVIDER)
"""
import json

from classifier import followup_signal, keyword_intent
from llm import LLMReply, ToolCall

AREA_FOR_INTENT = {"vpn": "vpn", "email": "email", "network": "network", "device_compliance": "device"}


def _call(name, **args):
    return LLMReply("", [ToolCall("c_" + name, name, args)])


class RuleModel:
    name = "rule-model (offline stand-in)"

    def chat(self, messages, tools):
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user" and "Now write your final reply" not in m["content"])
        user = messages[last_user]["content"]
        seen = [(m["name"], json.loads(m["content"])) for m in messages[last_user + 1:] if m["role"] == "tool"]
        calls = {(n, o.get("area") or o.get("action") or o.get("service")): o for n, o in seen}
        intent = keyword_intent(user)
        area = AREA_FOR_INTENT.get(intent)

        if not tools or (seen and self._finished(seen, intent, area)):
            return LLMReply(self._final(seen, intent, user))
        if intent == "password":
            return _call("send_password_reset_link", area="account") if not seen else LLMReply(self._final(seen, intent, user))
        if intent == "software":
            return _call("install_software", application=user)
        if intent == "security":
            return _call("secure_account", reason=user)
        if intent in {"hardware", "safety"}:
            return _call("create_ticket", area="hardware", title=f"Hardware fault: {user[:80]}",
                         reason="Physical hardware needs inspection, repair or replacement by a technician.")
        if intent == "how_to":
            return _call("search_knowledge", query=user)
        if intent == "service_status":
            return _call("check_service_status", service="vpn")
        if intent == "ticket_status":
            return _call("lookup", what="tickets")
        if intent == "list_devices":
            return _call("lookup", what="devices")
        if intent == "unknown" and followup_signal(user) == "positive":
            import re
            open_area = re.search(r"\n  - (\w+):", messages[0]["content"])
            if open_area and "mark_resolved" not in dict(seen):
                return _call("mark_resolved", area=open_area.group(1))
            return LLMReply("Great, glad it's working. Anything else?")
        if area is None:
            return LLMReply("Which app or device is affected, and what exact error or behaviour do you see?")
        if ("run_diagnostics", area) not in calls:
            return _call("run_diagnostics", area=area)
        findings = calls[("run_diagnostics", area)]
        for obs in [o for n, o in seen if n in {"run_diagnostics", "apply_fix"}][::-1][:1]:
            data = obs.get("verification", obs)
            for check in data.get("failed", []):
                if check["fix"] and ("apply_fix", check["fix"]) not in calls:
                    return _call("apply_fix", action=check["fix"])
            for check in data.get("failed", []):
                if "employee" in check["who_must_act"] and "send_password_reset_link" not in dict(seen):
                    return _call("send_password_reset_link", area=area)
            if data.get("all_passed") and "search_knowledge" not in dict(seen) and "apply_fix" not in dict(seen):
                return _call("search_knowledge", query=user)
        return LLMReply(self._final(seen, intent, user))

    @staticmethod
    def _finished(seen, intent, area):
        names = [n for n, _ in seen]
        if intent in {"password", "software", "security", "hardware", "safety", "how_to", "service_status", "ticket_status", "list_devices"}:
            return True
        return False

    @staticmethod
    def _final(seen, intent, user):
        if intent == "greeting":
            return "Hi! Tell me what isn't working and I'll investigate and fix it."
        parts = []
        for name, obs in seen:
            if name == "apply_fix" and obs.get("applied"):
                parts.append(obs["detail"] + ".")
            if name == "install_software":
                parts.append(f"I installed {obs['application']}." if obs.get("installed") else obs.get("reason", ""))
            if name == "search_knowledge" and obs.get("found"):
                parts.append("From our knowledge base (" + obs["source"] + "): " + obs["content"][:400])
            if name == "check_service_status":
                parts.append(f"The {obs['service']} service is {obs['status']}.")
        last = [o for n, o in seen if n in {"run_diagnostics", "apply_fix"}]
        if last:
            data = last[-1].get("verification", last[-1])
            if data.get("failed"):
                parts.append("Still failing: " + "; ".join(c["problem"] for c in data["failed"]) + ".")
            elif any(n == "apply_fix" for n, _ in seen):
                parts.append("I re-checked and everything passes now. Please try again and tell me if it works.")
            else:
                parts.append("All my automated checks pass. Can you tell me the exact error message you see?")
        return " ".join(p for p in parts if p) or "Let me know what you need."
