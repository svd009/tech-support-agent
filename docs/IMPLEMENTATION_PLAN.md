# Version 1 implementation plan

Goal: complete the agreed single-agent learning application in one downloadable package.
Architecture: FastAPI, SQLite, authenticated employee context, plain Python tool loop, Ollama.
User authorized direct completion; no further design gate. Preserve existing four table definitions.

- [x] Persistence and authentication: database.py, auth.py, seed.py; test login, expiry, logout and owner isolation.
- [x] Tools: tools.py, retrieval.py, tickets.py, schemas.py; test validated lookup, retrieval, draft confirmation and idempotency.
- [x] Agent: model_client.py, agent.py, prompts/support_agent.md; test structured calls, malformed calls, model failures, limits and stored history.
- [x] API and client: main.py, client.py; test full authenticated chat/draft/confirm/read flow and support-only updates.
- [x] Delivery: configuration, Docker, CI, README, 20 live evaluation scenarios; run complete suite, compile and package.

Review focus: model-generated employee IDs must be rejected; ticket creation requires confirmation outside the model; no raw text tool execution; no cross-user sessions; existing SQLite data must survive initialization. Local MVP, single API worker, no production-readiness claim. MCP, hybrid RAG, SSO and advanced monitoring remain later milestones.

Verification scope and untested runtime integrations: see VERIFICATION.md.
