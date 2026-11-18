"""Environment configuration. Restart the application after changing values."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("SUPPORT_DB", str(ROOT / "data" / "support.db")))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:0.6b")
MODEL_TIMEOUT = float(os.getenv("MODEL_TIMEOUT", "60"))
MODEL_CONTEXT = int(os.getenv("MODEL_CONTEXT", "4096"))
MAX_MODEL_CALLS = 4
MAX_TOOL_CALLS = 8
TOKEN_LIFETIME = 3600

# --- v3 model-driven agent -------------------------------------------------
# AGENT_MODE: "auto" (use the model, fall back to playbooks if unreachable), "model" (never fall back),
#             "playbook" (deterministic v2 engine only).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")            # ollama | anthropic
AGENT_MODEL = os.getenv("AGENT_MODEL", "")                     # default depends on provider
AGENT_NUM_CTX = int(os.getenv("AGENT_NUM_CTX", "6144"))
AGENT_MODEL_TIMEOUT = float(os.getenv("AGENT_MODEL_TIMEOUT", "240"))
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))       # model calls per employee message
AGENT_MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "14"))
