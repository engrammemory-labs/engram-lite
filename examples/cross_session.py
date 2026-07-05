"""End-to-end cross-session example/test.

Simulates two AI sessions sharing ONE memory file. Run as two SEPARATE processes
so the sharing is proven to be via the file, not in-memory state:

    python examples/cross_session.py write   # "session A" (e.g. Claude) saves facts
    python examples/cross_session.py read    # "session B" (e.g. Cursor) recalls them

Uses the real local embedder, so session B can ask a *differently-worded* question
and still recall what session A saved.
"""
import os
import sys

from engram import Memory
from engram.embeddings import HashEmbedder

DB = "/tmp/engram_e2e.db"


def _embedder():
    # ENGRAM_EMBEDDER=stub uses the dependency-free hash embedder (no model download).
    if os.environ.get("ENGRAM_EMBEDDER", "").lower() in ("stub", "hash"):
        return HashEmbedder()
    return None  # default → the real local model


def write() -> None:
    mem = Memory(DB, embedder=_embedder(), origin_tool="claude")
    print("── session A (claude) — writing ──\n")
    turns = [
        "We decided to use pgx + sqlc, not an ORM, for the payments service.",
        "I have a meeting at 5:30am tomorrow about the Q3 roadmap.",
        "The payments service is owned by Bob.",
        "what database should I use here?",                       # question → skip
        "def add(a, b):\n    return a + b\n",                     # code → skip
        "my openai key is sk-abcdefghijklmnopqrstuvwx, keep it",  # secret → redact
    ]
    for t in turns:
        res = mem.remember(t)
        line = f"  [{res['decision']:8}] {t[:46]!r}"
        if res.get("redacted"):
            line += f"  redacted={res['redacted']}"
        if res["decision"] == "SKIP":
            line += f"  ({res['reason']})"
        print(line)
    print("\n  stats:", mem.stats())
    mem.close()


def read() -> None:
    mem = Memory(DB, embedder=_embedder(), origin_tool="cursor")
    print("── session B (cursor) — recalling (different wording) ──\n")
    for q in [
        "what DB layer should I use in the payments repo?",
        "what's on my schedule?",
        "who owns the payments service?",
    ]:
        hits = mem.search(q)
        print(f"  Q: {q}")
        if not hits:
            print("     (nothing recalled)")
        for h in hits:
            print(f"     - {h['value']}   (written by {h['origin_tool']})")
        print()
    mem.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"write": write, "read": read}.get(cmd, lambda: print("usage: cross_session.py write|read"))()
