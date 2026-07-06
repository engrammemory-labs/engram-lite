"""Read-only store inspection shared by `status`, `diagnose`, and `doctor`.

Everything here opens SQLite directly and never constructs a Memory (no
embedding model load, no writes, safe against a running daemon — WAL
readers don't block). Integration configs are probed by path only; this
module knows where adapters keep their stores, not how they work.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional


def _hermes_home() -> str:
    return os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))


def discover_stores() -> List[Dict[str, str]]:
    """Every store an integration on this machine might be using, labeled."""
    candidates: List[Dict[str, str]] = []

    env_db = os.environ.get("ENGRAM_DB_PATH")
    if env_db:
        candidates.append({"label": "ENGRAM_DB_PATH", "path": os.path.expanduser(env_db)})

    candidates.append({"label": "standalone default",
                       "path": os.path.expanduser("~/.engram/memory.db")})

    hermes_db = os.path.join(_hermes_home(), "engram", "memory.db")
    hermes_cfg = os.path.join(_hermes_home(), "engram", "config.json")
    if os.path.exists(hermes_db) or os.path.exists(hermes_cfg):
        candidates.append({"label": "Hermes provider", "path": hermes_db})

    claude_cfg = os.path.expanduser(
        os.environ.get("ENGRAM_CLAUDE_CONFIG", "~/.engram/claude.json"))
    if os.path.exists(claude_cfg):
        try:
            with open(claude_cfg, "r", encoding="utf-8") as f:
                db = json.load(f).get("db")
            if isinstance(db, str) and db:
                candidates.append({"label": "Claude Code adapter",
                                   "path": os.path.expanduser(db)})
        except (OSError, ValueError):
            pass

    seen, unique = set(), []
    for c in candidates:
        if c["path"] in seen:
            continue
        seen.add(c["path"])
        unique.append(c)
    return unique


def store_stats(path: str) -> Optional[Dict[str, Any]]:
    """Facts, profiles, and decision counts — or None if no store there."""
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        stats: Dict[str, Any] = {"path": path}
        stats["current"] = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL "
            "AND validation_status = 'fresh'").fetchone()[0]
        stats["total"] = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        try:
            stats["cap"] = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'max_facts'").fetchone()
            stats["cap"] = int(stats["cap"][0]) if stats["cap"] else None
        except sqlite3.Error:
            stats["cap"] = None
        stats["profiles"] = [
            {"agent": r[0], "persona": r[1], "domain": r[2]}
            for r in conn.execute(
                "SELECT agent, persona, domain FROM profiles ORDER BY agent")]
        stats["decision_counts"] = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM (SELECT kind FROM decisions "
            "ORDER BY id DESC LIMIT 50) GROUP BY kind").fetchall())
        stats["decisions_total"] = conn.execute(
            "SELECT COUNT(*) FROM decisions").fetchone()[0]
        return stats
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def recent_decisions(path: str, limit: int = 10,
                     kind: Optional[str] = None) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        if kind:
            rows = conn.execute(
                "SELECT ts, kind, rule, snippet FROM decisions WHERE kind = ? "
                "ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, kind, rule, snippet FROM decisions "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "kind": r[1], "rule": r[2], "snippet": r[3] or ""}
                for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def daemon_state(path: str) -> Optional[Dict[str, Any]]:
    """The serve state file, if a daemon has published one for this store."""
    try:
        with open(path + ".serve.json", "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


# Plain-English explanations for the rules users actually hit. Anything
# unlisted falls back to showing the rule name itself.
SKIP_HINTS = {
    "question": "questions are not stored — state the fact instead "
                "(“the review is every second Tuesday”)",
    "noise": "the whole message was code/output/chatter with no durable fact in it",
    "artifact": "looked like machine output (logs, tracebacks), not something you said",
    "no task tags": "the query had no overlap with this agent's scope, so nothing was served",
}


def explain_rule(rule: str) -> str:
    low = rule.lower()
    for key, hint in SKIP_HINTS.items():
        if key in low:
            return hint
    return rule
