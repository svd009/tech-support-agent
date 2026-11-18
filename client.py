"""Interactive employee client. Ticket confirmation is performed outside the model."""
import getpass
import os
from uuid import UUID
import httpx


def main():
    base = os.getenv("SUPPORT_API", "http://127.0.0.1:8000")
    with httpx.Client(base_url=base, timeout=300, trust_env=False) as client:
        response = client.post("/auth/login", json={"employee_id": input("Employee ID: ").strip(), "password": getpass.getpass("Password: ")})
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        session_id = None
        print("New conversation started. Enter your support question at You:.")
        print("Commands: /quit, /new, /tickets, /resume <session-id>.")
        print("All data is simulated. Never enter passwords or MFA codes into chat.")
        while True:
            question = input("You: ").strip()
            if question == "/quit":
                client.post("/auth/logout")
                break
            if question == "/new":
                session_id = None
                print("New conversation started.")
                continue
            try:
                if question.startswith("/resume"):
                    parts = question.split(maxsplit=1)
                    try:
                        requested_id = str(UUID(parts[1])) if len(parts) == 2 else None
                    except ValueError:
                        requested_id = None
                    if requested_id is None:
                        print("Use /resume followed by a session UUID printed by this client.")
                        continue
                    history = client.get(f"/sessions/{requested_id}")
                    history.raise_for_status()
                    session_id = requested_id
                    print(f"Resumed session: {session_id}")
                    continue
                if question == "/tickets":
                    response = client.get("/tickets")
                    response.raise_for_status()
                    for ticket in response.json():
                        print(f"#{ticket['id']} {ticket['status']}: {ticket['title']}")
                    continue
                if not question:
                    continue
                response = client.post("/chat", json={"message": question, "session_id": session_id})
                response.raise_for_status()
                result = response.json()
                session_id = result["session_id"]
                print(f"Session: {session_id}")
                print("Agent:", result["answer"])
                for link in result.get("reset_links", []):
                    print(f"Open this one-time link in your browser: {base}{link}")
                print("Outcome:", result["outcome"])
                print("Tools used:", ", ".join(item["tool"] for item in result["tool_results"]) or "none")
                if result["sources"]:
                    print("Retrieved sources:", ", ".join(result["sources"]))
                for draft in result["pending_tickets"]:
                    print(f"Ticket draft: {draft['title']}\n{draft['description']}")
                    if input("Create this ticket? Type yes to confirm: ").strip().lower() == "yes":
                        confirmation = client.post(f"/ticket-drafts/{draft['id']}/confirm")
                        confirmation.raise_for_status()
                        print(f"Created ticket #{confirmation.json()['id']}. Saved to the local IT support queue.")
            except httpx.HTTPStatusError as error:
                print("API error:", error.response.status_code, error.response.text)
                if error.response.status_code == 401:
                    print("Restart this client to sign in again.")
                    break
            except httpx.RequestError as error:
                print("Connection error:", type(error).__name__)
                print("Inspect the session before repeating ticket actions; the server may still be processing.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as error:
        print("Could not start client:", error)
