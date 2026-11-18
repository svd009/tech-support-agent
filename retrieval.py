"""Small lexical baseline. Documents are fictional and approved for every demo user."""
import re
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
STOP_WORDS = set("a an the is are my our your i me what how can should do to for with and of it says please help".split())


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP_WORDS


def search_knowledge_base(query: str) -> dict:
    if not isinstance(query, str):
        raise TypeError("query must be a string.")
    if not query.strip():
        raise ValueError("query must not be blank.")
    matches = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        score = len(tokenize(query) & tokenize(content))
        if score:
            matches.append({"source": path.name, "content": content[:4000], "score": score})
    matches.sort(key=lambda item: item["score"], reverse=True)
    return {"results": matches[:2]}


if __name__ == "__main__":
    print(search_knowledge_base("VPN authentication failure"))
