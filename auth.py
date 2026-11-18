"""Local password authentication with hashed, expiring opaque bearer tokens."""
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import TOKEN_LIFETIME
from database import connect

bearer = HTTPBearer(auto_error=False)
ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"{ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    iterations, salt, expected = encoded.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(actual.hex(), expected)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_employee(employee_id: str, name: str, password: str, role="employee"):
    if role not in {"employee", "it_support"} or not employee_id.strip() or not name.strip():
        raise ValueError("Invalid employee identity or role.")
    if not 12 <= len(password) <= 256:
        raise ValueError("Use a password between 12 and 256 characters.")
    encoded = hash_password(password)
    with connect() as db:
        db.execute("INSERT INTO employees (id, name, role) VALUES (?, ?, ?)", (employee_id, name, role))
        db.execute("INSERT INTO credentials VALUES (?, ?)", (employee_id, encoded))


def login(employee_id: str, password: str) -> dict:
    now = int(time.time())
    with connect() as db:
        attempt = db.execute("SELECT * FROM login_attempts WHERE employee_id=?", (employee_id,)).fetchone()
        if attempt and attempt["blocked_until"] > now:
            raise HTTPException(429, "Too many login attempts. Try again in one minute.")
        record = db.execute("SELECT password_hash FROM credentials WHERE employee_id=?", (employee_id,)).fetchone()
    # A dummy derivation reduces the obvious timing difference for unknown accounts.
    encoded = record["password_hash"] if record else f"{ITERATIONS}${'00' * 16}${'00' * 32}"
    valid = verify_password(password, encoded) and record is not None
    if not valid:
        with connect() as db:
            failures = attempt["failures"] + 1 if attempt and not attempt["blocked_until"] else 1
            db.execute("""INSERT INTO login_attempts VALUES (?, ?, ?)
                ON CONFLICT(employee_id) DO UPDATE SET failures=excluded.failures,
                blocked_until=excluded.blocked_until""", (employee_id, failures, now + 60 if failures >= 5 else 0))
        raise HTTPException(401, "Invalid employee ID or password.")
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("DELETE FROM login_attempts WHERE employee_id=?", (employee_id,))
        db.execute("DELETE FROM auth_tokens WHERE expires_at<=?", (now,))
        db.execute("INSERT INTO auth_tokens VALUES (?, ?, ?)", (token_digest(token), employee_id, now + TOKEN_LIFETIME))
    return {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_LIFETIME}


def current_employee(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Authentication required.", headers={"WWW-Authenticate": "Bearer"})
    with connect() as db:
        row = db.execute("""SELECT e.id, e.name, e.role FROM auth_tokens t
            JOIN employees e ON e.id=t.employee_id
            WHERE t.token_hash=? AND t.expires_at>?""", (token_digest(credentials.credentials), int(time.time()))).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid or expired token.", headers={"WWW-Authenticate": "Bearer"})
    return dict(row)


def change_password(employee_id: str, password: str, new_password: str):
    if password == new_password:
        raise HTTPException(400, "Choose a different new password.")
    # Reuse login throttling and credential verification. Revoke all tokens after changing.
    login(employee_id, password)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT password_hash FROM credentials WHERE employee_id=?", (employee_id,)).fetchone()
        if not current or not verify_password(password, current["password_hash"]):
            raise HTTPException(401, "Credentials changed. Sign in again.")
        db.execute("UPDATE credentials SET password_hash=? WHERE employee_id=?", (hash_password(new_password), employee_id))
        db.execute("DELETE FROM auth_tokens WHERE employee_id=?", (employee_id,))
        db.execute("DELETE FROM password_resets WHERE employee_id=?", (employee_id,))
    return {"message": "Password changed. Sign in again with your new password."}


def issue_reset_link(employee_id: str, technician: dict) -> dict:
    if technician["role"] != "it_support":
        raise HTTPException(403, "IT support role required.")
    token = secrets.token_urlsafe(32)
    with connect() as db:
        if not db.execute("SELECT 1 FROM credentials WHERE employee_id=?", (employee_id,)).fetchone():
            raise HTTPException(404, "Account not found.")
        db.execute("DELETE FROM password_resets WHERE employee_id=?", (employee_id,))
        db.execute("INSERT INTO password_resets VALUES (?, ?, ?)", (token_digest(token), employee_id, int(time.time()) + 900))
    # Fragment keeps the secret out of server URL/access logs.
    return {"reset_path": "/reset-password#token=" + token, "expires_in": 900}


def redeem_reset(token: str, new_password: str) -> dict:
    encoded = hash_password(new_password)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        record = db.execute("SELECT * FROM password_resets WHERE token_hash=? AND expires_at>?", (token_digest(token), int(time.time()))).fetchone()
        if not record:
            raise HTTPException(400, "Reset link is invalid or expired. Ask the technician for a new one.")
        employee_id = record["employee_id"]
        db.execute("UPDATE credentials SET password_hash=? WHERE employee_id=?", (encoded, employee_id))
        db.execute("DELETE FROM password_resets WHERE employee_id=?", (employee_id,))
        db.execute("DELETE FROM auth_tokens WHERE employee_id=?", (employee_id,))
        db.execute("DELETE FROM login_attempts WHERE employee_id=?", (employee_id,))
    return {"message": "Password reset. Sign in with your new password."}


def request_recovery(employee_id: str) -> dict:
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        known = db.execute("SELECT 1 FROM credentials WHERE employee_id=?", (employee_id,)).fetchone()
        previous = db.execute("SELECT requested_at FROM recovery_requests WHERE employee_id=?", (employee_id,)).fetchone()
        if known and (not previous or previous["requested_at"] < now - 3600):
            db.execute("INSERT INTO recovery_requests VALUES (?, ?) ON CONFLICT(employee_id) DO UPDATE SET requested_at=excluded.requested_at", (employee_id, now))
            db.execute("INSERT INTO tickets(employee_id,title,description) VALUES (?, ?, ?)", (employee_id, "Account recovery requested", "Unauthenticated recovery request. Verify the employee identity through a trusted channel before issuing a reset link. This request is not identity verification."))
    return {"message": "If the account exists, a recovery request has been sent to IT support. A technician must verify your identity and provide a reset link. No email has been sent. Repeated requests within an hour are combined."}


# ---------------------------------------------------------------- corporate password self-service
# The chat agent issues these links for the AUTHENTICATED employee only (their bearer token is
# the identity proof). The link resets the simulated corporate directory password used by
# VPN, email and Windows sign-in. It never resets another employee's password.
DIRECTORY_RESET_LIFETIME = 900


def password_policy_problem(password: str) -> str | None:
    if not 12 <= len(password) <= 256:
        return "Use 12 to 256 characters."
    classes = sum([any(c.islower() for c in password), any(c.isupper() for c in password),
                   any(c.isdigit() for c in password), any(not c.isalnum() for c in password)])
    if classes < 3:
        return "Use at least three of: lowercase, uppercase, digits, symbols."
    return None


def issue_directory_reset(employee_id: str, session_id: str | None) -> dict:
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("DELETE FROM directory_resets WHERE employee_id=?", (employee_id,))
        db.execute("INSERT INTO directory_resets VALUES (?, ?, ?, ?)",
                   (token_digest(token), employee_id, session_id, int(time.time()) + DIRECTORY_RESET_LIFETIME))
    # Fragment keeps the secret out of server URL/access logs.
    return {"reset_path": "/corporate-password#token=" + token, "expires_in": DIRECTORY_RESET_LIFETIME}


def redeem_directory_reset(token: str, new_password: str) -> dict:
    from it_systems import get_state, save_state  # local import avoids a cycle at startup
    problem = password_policy_problem(new_password)
    if problem:
        raise HTTPException(400, problem)
    with connect() as db:
        peek = db.execute("SELECT employee_id FROM directory_resets WHERE token_hash=? AND expires_at>?",
                          (token_digest(token), int(time.time()))).fetchone()
    if peek:  # reject reuse BEFORE the one-time link is consumed, so the employee can simply try again
        directory = get_state(peek["employee_id"]).get("directory", {})
        old = list(directory.get("password_history", [])) + ([directory["password_hash"]] if directory.get("password_hash") else [])
        with connect() as db:
            current = db.execute("SELECT password_hash FROM credentials WHERE employee_id=?", (peek["employee_id"],)).fetchone()
        if current:
            old.append(current["password_hash"])  # the password they sign in with today counts as "old" too
        if any(verify_password(new_password, h) for h in old):
            raise HTTPException(400, "You can't reuse one of your last 5 passwords. Choose a new one and try again; "
                                     "your reset link is still valid.")
    now = int(time.time())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        record = db.execute("SELECT * FROM directory_resets WHERE token_hash=? AND expires_at>?",
                            (token_digest(token), now)).fetchone()
        if not record:
            raise HTTPException(400, "This reset link is invalid, used or expired. Ask the support agent for a new one.")
        db.execute("DELETE FROM directory_resets WHERE employee_id=?", (record["employee_id"],))
    state = get_state(record["employee_id"])
    kept = (list(state["directory"].get("password_history", [])) +
            ([state["directory"]["password_hash"]] if state["directory"].get("password_hash") else []))[-4:]
    state["directory"]["password_history"] = kept  # with the new hash below: the last 5 passwords are remembered
    state["directory"].update(password_expired=False, locked=False, failed_logins=0,
                              password_changed_at=now, password_hash=hash_password(new_password))
    save_state(record["employee_id"], state)
    with connect() as db:  # one corporate identity: the new password is also the sign-in password for this app
        db.execute("UPDATE credentials SET password_hash=? WHERE employee_id=?",
                   (state["directory"]["password_hash"], record["employee_id"]))
        db.execute("DELETE FROM login_attempts WHERE employee_id=?", (record["employee_id"],))
    return {"message": "Your corporate password has been changed. Return to the support chat and tell the agent you're done."}
