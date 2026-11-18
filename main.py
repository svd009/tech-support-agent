"""Authenticated local API. Run with one worker for this SQLite learning MVP."""
import json
import logging
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from agent import run_agent
from auth import redeem_directory_reset, request_recovery, issue_reset_link, redeem_reset, change_password, bearer, current_employee, login, token_digest
from database import connect, initialize_database
from schemas import DirectoryResetInput, RecoveryRequestInput, PasswordResetInput, ResetLinkInput, PasswordChangeInput, ChatInput, LoginInput, ServiceStatusResponse, TicketUpdate
from sessions import get_session, list_sessions
from tickets import session_tickets, confirm_draft, list_tickets, get_ticket, update_ticket, pending_drafts
from tools import check_service_status

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("support")
chat_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    initialize_database()
    import os
    import threading
    if os.getenv("AGENT_MODE", "pipeline").lower() == "pipeline" and os.getenv("UNDERSTAND_WITH_MODEL", "1") != "0":
        import model_client  # load the model in the background so the first unclear message is not slow
        threading.Thread(target=model_client.warm_up, daemon=True).start()
    import reply_writer
    logger.info(json.dumps({"event": "reply_writer", "provider": reply_writer.provider(), "model": reply_writer.model_id()}))
    print(f"Reply writer: {reply_writer.provider()}  model: {reply_writer.model_id() or '(built-in templates)'}", flush=True)
    yield


app = FastAPI(title="Tech Support Agent — simulated demo", lifespan=lifespan)
WEB_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id = str(uuid4())
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    logger.info(json.dumps({"event": "request", "request_id": request_id,
        "method": request.method, "path": request.url.path, "status": response.status_code,
        "duration_ms": round((time.monotonic() - started) * 1000)}))
    return response


@app.get("/", include_in_schema=False)
def web_interface():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "database": "ok", "simulated": True, "model_checked": False}


@app.post("/auth/login")
def sign_in(body: LoginInput):
    return login(body.employee_id, body.password)


@app.post("/auth/logout", status_code=204)
def sign_out(employee: dict = Depends(current_employee), credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    with connect() as db:
        db.execute("DELETE FROM auth_tokens WHERE token_hash=?", (token_digest(credentials.credentials),))
    return Response(status_code=204)


@app.get("/me")
def me(employee: dict = Depends(current_employee)):
    return employee


@app.get("/services/{service_name}/status", response_model=ServiceStatusResponse)
def service_status(service_name: str, employee: dict = Depends(current_employee)):
    try:
        return check_service_status(service_name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/agent/status")
def agent_status():
    """Which engine answers chats, and whether the model is reachable (no employee data involved)."""
    import os
    import config
    from llm import get_llm, LLMError
    mode = os.getenv("AGENT_MODE", "pipeline").lower()
    info = {"mode": mode, "provider": os.getenv("LLM_PROVIDER", config.LLM_PROVIDER)}
    if mode == "playbook":
        return {**info, "engine": "playbook (no model used)"}
    if mode == "pipeline":
        import model_client
        import reply_writer
        return {**info, "engine": "pipeline (code investigates; model writes the reply)" if reply_writer.enabled()
                else "pipeline (code investigates; templated replies)",
                "reply_provider": reply_writer.provider(), "reply_model": reply_writer.model_id() or None,
                "understand_model": model_client._model(), "understand_timeout_s": model_client._timeout()}
    try:
        model = get_llm()
        info["model"] = getattr(model, "model", getattr(model, "name", "?"))
        if info["provider"] == "ollama":
            import httpx
            tags = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=3, trust_env=False).json()
            names = [m["name"] for m in tags.get("models", [])]
            info["model_installed"] = any(n == info["model"] or n.split(":")[0] == info["model"] for n in names) or info["model"] in names
            info["reachable"] = True
        else:
            info["reachable"] = bool(os.getenv("ANTHROPIC_API_KEY"))
    except (LLMError, Exception) as error:
        info.update(reachable=False, problem=str(error)[:200])
    return info


@app.get("/chat/progress")
def chat_progress(employee: dict = Depends(current_employee)):
    """What the agent is doing right now for this employee's request (polled by the chat page)."""
    import progress
    return progress.get(employee["id"])


@app.post("/chat")
def chat(body: ChatInput, employee: dict = Depends(current_employee)):
    # Serial execution protects session ordering and limits local model load.
    if not chat_lock.acquire(blocking=False):
        raise HTTPException(409, "Another chat is running. Please retry when it finishes.")
    try:
        return run_agent(body.message, employee, body.session_id)
    finally:
        chat_lock.release()


@app.get("/sessions")
def conversations(employee: dict = Depends(current_employee)):
    return list_sessions(employee["id"])


@app.get("/sessions/{session_id}")
def session(session_id: str, employee: dict = Depends(current_employee)):
    result = get_session(session_id, employee["id"])
    result["pending_tickets"] = pending_drafts(employee["id"], session_id)
    result["created_tickets"] = session_tickets(employee["id"], session_id)
    return result


@app.post("/ticket-drafts/{draft_id}/confirm")
def confirm(draft_id: str, employee: dict = Depends(current_employee)):
    return confirm_draft(draft_id, employee["id"])


@app.get("/tickets")
def tickets(employee: dict = Depends(current_employee)):
    return list_tickets(employee)


@app.get("/tickets/{ticket_id}")
def ticket(ticket_id: int, employee: dict = Depends(current_employee)):
    return get_ticket(ticket_id, employee)


@app.patch("/tickets/{ticket_id}")
def change_ticket(ticket_id: int, body: TicketUpdate, employee: dict = Depends(current_employee)):
    return update_ticket(ticket_id, body.status, employee)


@app.get("/reset-password", include_in_schema=False)
def password_page():
    return FileResponse(WEB_DIR / "reset-password.html")


@app.post("/auth/change-password")
def password_change(body: PasswordChangeInput):
    return change_password(body.employee_id, body.password, body.new_password)


@app.post("/auth/reset-link")
def reset_link(body: ResetLinkInput, employee: dict = Depends(current_employee)):
    return issue_reset_link(body.employee_id, employee)


@app.post("/auth/reset-password")
def reset_password(body: PasswordResetInput):
    return redeem_reset(body.token, body.new_password)


@app.post("/auth/request-recovery")
def recovery_request(body: RecoveryRequestInput):
    return request_recovery(body.employee_id)


@app.get("/corporate-password", include_in_schema=False)
def corporate_password_page():
    return FileResponse(WEB_DIR / "corporate-password.html")


@app.post("/auth/corporate-password/reset")
def corporate_password_reset(body: DirectoryResetInput):
    # The one-use token (issued by the agent to the signed-in employee) is the authorisation.
    return redeem_directory_reset(body.token, body.new_password)


@app.get("/demo/symptoms")
def demo_symptoms(employee: dict = Depends(current_employee)):
    # Demo only: what the signed-in employee is experiencing this round. Never the cause.
    from incidents import symptoms_for
    return {"symptoms": symptoms_for(employee["id"])}
