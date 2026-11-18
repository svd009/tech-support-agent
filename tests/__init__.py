import os

# The suite must never depend on, or talk to, whatever model the developer has configured in their shell.
# These tests exercise the deterministic playbook engine; the model-loop tests install their own scripted
# models and switch the mode on explicitly.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(name, None)
