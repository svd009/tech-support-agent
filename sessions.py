"""Persist complete messages. Session access is always owner-only."""
import json
from uuid import uuid4
from fastapi import HTTPException
from database import connect


def list_sessions(employee_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("""SELECT s.id, s.created_at,
            (SELECT payload FROM messages WHERE session_id=s.id ORDER BY id LIMIT 1) AS first_message
            FROM sessions s WHERE s.employee_id=? ORDER BY s.created_at DESC, s.rowid DESC LIMIT 50""",
            (employee_id,)).fetchall()
    return [{"id": row["id"], "created_at": row["created_at"],
             "title": json.loads(row["first_message"] or '{}').get("content", "New conversation")[:90]}
            for row in rows]


def get_session(session_id: str, employee_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=? AND employee_id=?", (session_id, employee_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Session not found.")
        messages = [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM messages WHERE session_id=? ORDER BY id", (session_id,))]
    return {"id": row["id"], "messages": messages}


def create_session(employee_id: str) -> str:
    session_id = str(uuid4())
    with connect() as db:
        db.execute("INSERT INTO sessions (id, employee_id) VALUES (?, ?)", (session_id, employee_id))
    return session_id


def save_turn(session_id: str, messages: list[dict]):
    with connect() as db:
        db.executemany("INSERT INTO messages (session_id, payload) VALUES (?, ?)",
                       [(session_id, json.dumps(message)) for message in messages])


def recent_history(messages: list[dict]) -> list[dict]:
    # Keep whole turns so tool results are never separated from their requests.
    starts = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    history = messages[starts[max(0, len(starts) - 3)]:] if starts else []
    while len(json.dumps(history)) > 16000 and sum(m.get("role") == "user" for m in history) > 1:
        next_user = next(i for i, m in enumerate(history[1:], 1) if m.get("role") == "user")
        history = history[next_user:]
    return history
