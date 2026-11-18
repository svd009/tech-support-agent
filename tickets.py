"""Owner-scoped tickets: confirmed drafts and automatic policy-based escalations."""
import json
import time
from uuid import uuid4
from fastapi import HTTPException
from database import connect
from support_policy import POLICY


def enrich_ticket(ticket: dict) -> dict:
    with connect() as db:
        details = db.execute("SELECT category, complexity, resolution_estimate, estimate_kind, creation_reason FROM ticket_details WHERE ticket_id=?", (ticket["id"],)).fetchone()
    if details:
        ticket.update(dict(details))
    return ticket


def ticket_id_update_marker() -> str:
    return "\n\n----- Update from support agent -----\n"


def escalate_issue(employee_id: str, session_id: str, title: str, description: str, category: str,
                   reason: str = "Issue requires technician action outside the agent's capabilities.") -> dict:
    policy = POLICY["categories"][category]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # A repeated escalation for this category in the same conversation reuses its open ticket.
        existing = db.execute("""SELECT t.id FROM tickets t JOIN ticket_details d ON d.ticket_id=t.id
            WHERE t.employee_id=? AND d.session_id=? AND d.category=? AND t.status!='resolved'
            ORDER BY t.id DESC LIMIT 1""", (employee_id, session_id, category)).fetchone()
        if existing:
            ticket_id, created = existing["id"], False
            # Keep one ticket per problem; append the newest findings for the technician.
            db.execute("""UPDATE tickets SET description=substr(description || ? || ?, 1, 12000),
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (ticket_id_update_marker(), description, ticket_id))
        else:
            cursor = db.execute("INSERT INTO tickets (employee_id, title, description) VALUES (?, ?, ?)", (employee_id, title, description))
            ticket_id, created = cursor.lastrowid, True
            db.execute("""INSERT INTO ticket_details
                (ticket_id, session_id, category, complexity, resolution_estimate, creation_reason)
                VALUES (?, ?, ?, ?, ?, ?)""", (ticket_id, session_id, category, policy["complexity"],
                policy["resolution_estimate"], reason))
        ticket = dict(db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())
    return {"ticket": enrich_ticket(ticket), "created": created, "automatic_escalation": True,
            "estimate_notice": POLICY["notice"]}


def prepare_ticket(employee_id: str, session_id: str, title: str, description: str) -> dict:
    with connect() as db:
        # Repeated calls with the same content reuse a pending draft.
        row = db.execute("""SELECT * FROM ticket_drafts WHERE employee_id=? AND session_id=?
            AND title=? AND description=? AND ticket_id IS NULL AND expires_at>?""",
            (employee_id, session_id, title, description, int(time.time()))).fetchone()
        if row:
            return dict(row)
        draft_id = str(uuid4())
        db.execute("INSERT INTO ticket_drafts VALUES (?, ?, ?, ?, ?, ?, NULL)",
                   (draft_id, employee_id, session_id, title, description, int(time.time()) + 3600))
        return dict(db.execute("SELECT * FROM ticket_drafts WHERE id=?", (draft_id,)).fetchone())


def pending_drafts(employee_id: str, session_id: str) -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT id, title, description, expires_at
            FROM ticket_drafts WHERE employee_id=? AND session_id=?
            AND ticket_id IS NULL AND expires_at>?""", (employee_id, session_id, int(time.time())))]


def confirm_draft(draft_id: str, employee_id: str) -> dict:
    with connect() as db:
        # Serialize confirmation so retries cannot create two tickets from one draft.
        db.execute("BEGIN IMMEDIATE")
        draft = db.execute("SELECT * FROM ticket_drafts WHERE id=? AND employee_id=?", (draft_id, employee_id)).fetchone()
        if draft is None:
            raise HTTPException(404, "Draft not found.")
        if draft["ticket_id"] is not None:
            return dict(db.execute("SELECT * FROM tickets WHERE id=?", (draft["ticket_id"],)).fetchone())
        if draft["expires_at"] <= int(time.time()):
            raise HTTPException(410, "Draft expired. Request a new draft.")
        cursor = db.execute("INSERT INTO tickets (employee_id, title, description) VALUES (?, ?, ?)",
                            (employee_id, draft["title"], draft["description"]))
        ticket_id = cursor.lastrowid
        db.execute("UPDATE ticket_drafts SET ticket_id=? WHERE id=?", (ticket_id, draft_id))
        confirmation = {"role": "assistant", "content": (
            f"Application confirmation: the employee confirmed draft {draft_id}. "
            f"Ticket #{ticket_id} was created in the local simulated support queue."
        )}
        db.execute("INSERT INTO messages (session_id, payload) VALUES (?, ?)",
                   (draft["session_id"], json.dumps(confirmation)))
        return dict(db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())


def get_ticket(ticket_id: int, employee: dict) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None or (row["employee_id"] != employee["id"] and employee["role"] != "it_support"):
        raise HTTPException(404, "Ticket not found.")
    return enrich_ticket(dict(row))


def list_tickets(employee: dict) -> list[dict]:
    with connect() as db:
        if employee["role"] == "it_support":
            rows = db.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT 100")
        else:
            rows = db.execute("SELECT * FROM tickets WHERE employee_id=? ORDER BY id DESC LIMIT 100", (employee["id"],))
        records = [dict(row) for row in rows]
    return [enrich_ticket(row) for row in records]


TICKET_UPDATE_TEXT = {
    "in_progress": ("Update on ticket #{id}: a technician has picked this up and is working on it. "
                    "You don't need to do anything right now. I'll post here when it changes."),
    "resolved": ("Update on ticket #{id}: the Tech Support team has marked this resolved. If the problem comes back "
                 "or isn't fully fixed, tell me here and I'll take it from there."),
    "open": "Update on ticket #{id}: it has been put back in the queue for a technician.",
}


def post_ticket_update(ticket_id: int, status: str) -> bool:
    """Write a visible status update into the conversation that created the ticket. Returns True if posted."""
    from uuid import uuid4
    import json
    with connect() as db:
        row = db.execute("SELECT session_id FROM ticket_details WHERE ticket_id=?", (ticket_id,)).fetchone() \
            or db.execute("SELECT session_id FROM ticket_drafts WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None or status not in TICKET_UPDATE_TEXT:
            return False
        payload = {"role": "assistant", "kind": "ticket_update", "uid": str(uuid4()), "ticket_id": ticket_id,
                   "status": status, "content": TICKET_UPDATE_TEXT[status].format(id=ticket_id)}
        db.execute("INSERT INTO messages (session_id, payload) VALUES (?, ?)", (row["session_id"], json.dumps(payload)))
    return True


def update_ticket(ticket_id: int, status: str, employee: dict) -> dict:
    if employee["role"] != "it_support":
        raise HTTPException(403, "Only IT support can change ticket status.")
    before = get_ticket(ticket_id, employee)["status"]
    with connect() as db:
        db.execute("UPDATE tickets SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, ticket_id))
    if status != before:
        post_ticket_update(ticket_id, status)  # the employee sees it in their chat
    return get_ticket(ticket_id, employee)


def session_tickets(employee_id: str, session_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("""SELECT DISTINCT t.* FROM tickets t WHERE t.employee_id=? AND
            (t.id IN (SELECT ticket_id FROM ticket_drafts WHERE session_id=? AND employee_id=?)
             OR t.id IN (SELECT ticket_id FROM ticket_details WHERE session_id=?)) ORDER BY t.id""",
            (employee_id, session_id, employee_id, session_id)).fetchall()
    return [enrich_ticket(dict(row)) for row in rows]
