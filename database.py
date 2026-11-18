"""SQLite connections and additive schema initialization."""
import sqlite3
from contextlib import contextmanager
from config import DATABASE_PATH


@contextmanager
def connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'employee'
                CHECK (role IN ('employee', 'it_support'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            description TEXT NOT NULL CHECK (length(trim(description)) > 0),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'in_progress', 'resolved')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_employee ON sessions(employee_id);
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
        CREATE INDEX IF NOT EXISTS idx_tickets_employee ON tickets(employee_id);
        CREATE TABLE IF NOT EXISTS credentials (
            employee_id TEXT PRIMARY KEY REFERENCES employees(id),
            password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token_hash TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            employee_id TEXT PRIMARY KEY, failures INTEGER NOT NULL,
            blocked_until INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticket_drafts (
            id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            session_id TEXT NOT NULL REFERENCES sessions(id),
            title TEXT NOT NULL, description TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            ticket_id INTEGER REFERENCES tickets(id)
        );
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            name TEXT NOT NULL, operating_system TEXT NOT NULL,
            compliant INTEGER NOT NULL CHECK (compliant IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS recovery_requests (
            employee_id TEXT PRIMARY KEY REFERENCES employees(id), requested_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_resets (
            token_hash TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES employees(id),
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticket_details (
            ticket_id INTEGER PRIMARY KEY REFERENCES tickets(id),
            session_id TEXT NOT NULL REFERENCES sessions(id),
            category TEXT NOT NULL, complexity TEXT NOT NULL,
            resolution_estimate TEXT NOT NULL,
            estimate_kind TEXT NOT NULL DEFAULT 'demo_target',
            creation_reason TEXT NOT NULL
        );
        -- Simulated enterprise systems (identity provider, VPN, mail, MDM). One JSON
        -- document per employee; in production each section maps to a real system API.
        CREATE TABLE IF NOT EXISTS employee_systems (
            employee_id TEXT PRIMARY KEY REFERENCES employees(id),
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS service_status (
            name TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('operational', 'degraded', 'down'))
        );
        INSERT OR IGNORE INTO service_status VALUES ('vpn', 'operational'), ('email', 'operational'), ('network', 'operational');
        -- Audit trail of every automated change the agent makes.
        CREATE TABLE IF NOT EXISTS remediation_log (
            id INTEGER PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            session_id TEXT,
            action TEXT NOT NULL,
            ok INTEGER NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- Per-conversation workflow state (what the agent is waiting for, attempts so far).
        CREATE TABLE IF NOT EXISTS session_state (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id),
            state TEXT NOT NULL
        );
        -- Org chart for the demo company (team, title, manager, location).
        CREATE TABLE IF NOT EXISTS employee_profiles (
            employee_id TEXT PRIMARY KEY REFERENCES employees(id),
            team TEXT NOT NULL, title TEXT NOT NULL, manager_id TEXT,
            location TEXT NOT NULL, start_date TEXT NOT NULL
        );
        -- Ground truth for random test rounds (incidents.py). The agent never reads this table.
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY, roll_id INTEGER NOT NULL,
            employee_id TEXT REFERENCES employees(id),
            fault TEXT NOT NULL, symptoms TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            ticket_floor INTEGER NOT NULL DEFAULT 0,  -- tickets/messages with ids above these
            message_floor INTEGER NOT NULL DEFAULT 0, -- belong to this round
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- One-use links for the employee's corporate (directory) password.
        CREATE TABLE IF NOT EXISTS directory_resets (
            token_hash TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            session_id TEXT,
            expires_at INTEGER NOT NULL
        );
        """)


if __name__ == "__main__":
    initialize_database()
    print(f"Database ready: {DATABASE_PATH}")
