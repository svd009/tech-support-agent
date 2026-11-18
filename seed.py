"""Create a local demo account. Passwords are entered privately, never hardcoded."""
import argparse
import getpass
import sqlite3
from auth import create_employee
from database import connect, initialize_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("employee_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=["employee", "it_support"], default="employee")
    args = parser.parse_args()
    password = getpass.getpass("Choose password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords differ. No account created.")
    initialize_database()
    try:
        create_employee(args.employee_id, args.name, password, args.role)
    except (ValueError, sqlite3.IntegrityError) as error:
        raise SystemExit(f"Account creation failed: {error}") from error
    with connect() as db:
        db.execute("INSERT INTO devices VALUES (?, ?, ?, ?, ?)",
                   (args.employee_id + "-laptop", args.employee_id, "Demo laptop", "Windows 11", 1))
    print(f"Created {args.employee_id} with a fictional device.")


if __name__ == "__main__":
    main()
