"""engram — a small, local-first agentic memory engine.

One SQLite file holds the facts, a keyword index (FTS5), and a vector index
(sqlite-vec). Meant to be shared across your AI tools (Claude, Cursor, …) and
across sessions. See docs/memory-docs/ for the design and docs/code-docs/ for the
code guide.

Quick start:

    from engram import Memory
    mem = Memory("my_memory.db")
    mem.save("Bob owns the payments service", subject="Bob")
    mem.search("who owns payments?", subject="Bob")

Layout:
    engram.core         the engine logic (memory, consolidation, retrieval, …)
    engram.storage      SQLite connection, schema, row/index operations
    engram.embeddings   text → vector (local model, with a stub fallback)
    engram.cli          the `engram` command (demo / status / rebuild)
"""

from .core.memory import Memory
from .embeddings import get_embedder

__all__ = ["Memory", "get_embedder"]
try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("engram-lite")
except Exception:  # not installed (running from a checkout)
    __version__ = "0.1.0"
