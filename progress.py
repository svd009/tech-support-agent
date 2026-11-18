"""Live progress for the chat page: what the agent is doing right now (in memory, per employee)."""
import threading
import time

_lock = threading.Lock()
_steps: dict[str, dict] = {}


def start(employee_id: str) -> None:
    with _lock:
        _steps[employee_id] = {"steps": [], "active": True, "started": time.time()}


def add(employee_id: str, text: str) -> None:
    with _lock:
        entry = _steps.setdefault(employee_id, {"steps": [], "active": True, "started": time.time()})
        entry["steps"].append({"t": round(time.time() - entry["started"], 1), "text": text})
        entry["steps"] = entry["steps"][-12:]


def finish(employee_id: str) -> None:
    with _lock:
        if employee_id in _steps:
            _steps[employee_id]["active"] = False


def get(employee_id: str) -> dict:
    with _lock:
        entry = _steps.get(employee_id)
        return {"active": False, "steps": []} if entry is None else {"active": entry["active"], "steps": list(entry["steps"])}
