"""Corvane Payments: a fictional demo company with 10 employees in 2 teams, plus one IT account.

Everyone starts with healthy systems. Problems are generated randomly by incidents.py, so nobody
(including you) knows in advance who is affected or why.

  python demo_company.py          create everyone (asks once for a shared demo password)
  python demo_company.py --list   show the roster

Set DEMO_PASSWORD in the environment to skip the password prompt. All data is fictional.
"""
import argparse
import getpass
import json
import os
import sqlite3

from auth import create_employee
from database import connect, initialize_database
from it_systems import DEFAULT_STATE, save_state, set_service_status

COMPANY = "Corvane Payments Inc."
TEAMS = {
    "payments-ops": "Payments Operations",
    "platform-eng": "Platform Engineering",
}

# id, name, team, title, manager, location, start date, (device name, OS)
EMPLOYEES = [
    ("maya.chen", "Maya Chen", "payments-ops", "Operations Team Lead", None, "New York office", "2021-03-15", ("ThinkPad T14", "Windows 11")),
    ("daniel.okafor", "Daniel Okafor", "payments-ops", "Payments Analyst", "maya.chen", "Remote (Austin, TX)", "2022-08-01", ("ThinkPad T14", "Windows 11")),
    ("sofia.ramirez", "Sofia Ramirez", "payments-ops", "Reconciliation Specialist", "maya.chen", "Hybrid (New York)", "2023-01-09", ("Latitude 5440", "Windows 11")),
    ("arjun.mehta", "Arjun Mehta", "payments-ops", "Fraud Analyst", "maya.chen", "New York office", "2022-11-14", ("ThinkPad T14", "Windows 11")),
    ("hannah.wright", "Hannah Wright", "payments-ops", "Customer Escalations Specialist", "maya.chen", "Remote (Chicago, IL)", "2026-11-16", ("Latitude 5440", "Windows 11")),
    ("leo.novak", "Leo Novak", "platform-eng", "Engineering Manager", None, "New York office", "2020-06-22", ("MacBook Pro 14", "macOS 15")),
    ("priya.nair", "Priya Nair", "platform-eng", "Senior Backend Engineer", "leo.novak", "Remote (Seattle, WA)", "2021-09-07", ("MacBook Pro 16", "macOS 15")),
    ("tom.becker", "Tom Becker", "platform-eng", "DevOps Engineer", "leo.novak", "Hybrid (New York)", "2022-02-28", ("XPS 15", "Windows 11")),
    ("aisha.karim", "Aisha Karim", "platform-eng", "Data Engineer", "leo.novak", "New York office", "2023-05-15", ("XPS 15", "Windows 11")),
    ("ben.carter", "Ben Carter", "platform-eng", "QA Engineer", "leo.novak", "Remote (Denver, CO)", "2024-04-01", ("Latitude 7450", "Windows 11")),
]
IT_ACCOUNT = ("itdesk", "Corvane IT Desk")

def build(password: str, quiet: bool = False):
    initialize_database()
    created = 0
    for emp_id, name, team, title, manager, location, start, (device, os_name) in EMPLOYEES:
        try:
            create_employee(emp_id, name, password)
            created += 1
        except sqlite3.IntegrityError:
            pass  # already exists: keep their password, refresh profile/device/problem
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO employee_profiles VALUES (?, ?, ?, ?, ?, ?)",
                       (emp_id, TEAMS[team], title, manager, location, start))
            db.execute("INSERT OR IGNORE INTO devices VALUES (?, ?, ?, ?, ?)",
                       (f"{emp_id}-laptop", emp_id, device, os_name, 1))
        save_state(emp_id, json.loads(json.dumps(DEFAULT_STATE)))  # healthy; incidents.py breaks things
    try:
        create_employee(*IT_ACCOUNT, password, role="it_support")
        created += 1
    except sqlite3.IntegrityError:
        pass
    for service in ("vpn", "email", "network"):
        set_service_status(service, "operational")
    if not quiet:
        print(f"{COMPANY}: {created} new accounts; all systems healthy.")


def show():
    for team_key, team in TEAMS.items():
        print(f"\n{team}")
        for emp_id, name, t, title, manager, location, *_ in EMPLOYEES:
            if t == team_key:
                print(f"  {emp_id:15} {name:15} {title:32} {location}")
    print(f"\nIT support login: {IT_ACCOUNT[0]}")
    print("Everyone starts healthy. Run 'python incidents.py roll' to start a random round.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        show()
        return
    password = os.getenv("DEMO_PASSWORD") or getpass.getpass("Shared demo password for all 11 accounts (12+ characters): ")
    if not os.getenv("DEMO_PASSWORD") and password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords differ. Nothing created.")
    build(password)
    show()


if __name__ == "__main__":
    main()
