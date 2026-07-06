"""All the SQL: row operations and the index queries.

This is the only module that writes SQL. Keeping it isolated (a "repository")
means the engine logic in core/ never touches SQL directly, so the storage can be
swapped or optimized later without changing the logic.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import List, Optional, Tuple

import sqlite_vec

from .. import config


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def today_iso() -> str:
    return _dt.date.today().isoformat()


# ── writes ────────────────────────────────────────────────────────────────────
def insert_fact(conn: sqlite3.Connection, *, fid: str, block_id: str, subject_key: str,
                key: str, value: str, fragment_type: str, confidence: float,
                created_at: str, valid_until: Optional[str], volatility_class: str,
                validation_status: str, origin_tool: Optional[str],
                prior_version_id: Optional[str], vec,
                entities: Optional[List[str]] = None,
                tags_json: str = "[]", domain: Optional[str] = None) -> None:
    """Insert the row + keyword entry + vector + entities, all in one transaction."""
    with conn:
        conn.execute(
            """INSERT INTO facts
               (id, block_id, subject_key, key, value, fragment_type, confidence,
                created_at, valid_until, volatility_class, validation_status,
                access_count, last_accessed_at, origin_tool, prior_version_id,
                superseded_by, tags, domain)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,NULL,?,?)""",
            (fid, block_id, subject_key, key, value, fragment_type, confidence,
             created_at, valid_until, volatility_class, validation_status,
             origin_tool, prior_version_id, tags_json, domain),
        )
        conn.execute("INSERT INTO facts_fts (fact_id, key) VALUES (?, ?)", (fid, key))
        conn.execute(
            "INSERT INTO facts_vec (fact_id, embedding) VALUES (?, ?)",
            (fid, sqlite_vec.serialize_float32(vec)),
        )
        for ent in entities or ():
            conn.execute(
                "INSERT INTO fact_entities (fact_id, entity) VALUES (?, ?)", (fid, ent)
            )


def mark_superseded(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, old_id))
    conn.commit()


def bump_confidence(conn: sqlite3.Connection, fid: str, amount: float) -> None:
    conn.execute(
        "UPDATE facts SET confidence = MIN(confidence + ?, 1.0) WHERE id = ?",
        (amount, fid),
    )
    conn.commit()


def invalidate(conn: sqlite3.Connection, fid: str) -> None:
    conn.execute("UPDATE facts SET validation_status = 'invalidated' WHERE id = ?", (fid,))
    conn.commit()


def touch_access(conn: sqlite3.Connection, fid: str) -> None:
    """Record that a fact was recalled (drives recency/usage for eviction)."""
    conn.execute(
        "UPDATE facts SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
        (now_iso(), fid),
    )
    conn.commit()


def update_tags(conn: sqlite3.Connection, fid: str, tags_json: str) -> None:
    conn.execute("UPDATE facts SET tags = ? WHERE id = ?", (tags_json, fid))
    conn.commit()


def sweep_expired(conn: sqlite3.Connection, today: str) -> int:
    """Mark past-their-expiry facts as dead so eviction can reclaim them."""
    cur = conn.execute(
        "UPDATE facts SET validation_status = 'expired' "
        "WHERE valid_until IS NOT NULL AND valid_until < ? "
        "AND validation_status = 'fresh' AND superseded_by IS NULL",
        (today,),
    )
    conn.commit()
    return cur.rowcount


# ── profiles (conditioned promotion) ─────────────────────────────────────────
def upsert_profile(conn: sqlite3.Connection, *, agent: str, persona: str,
                   domain: str, scope_tags_json: str) -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO profiles (agent, persona, domain, scope_tags, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(agent) DO UPDATE SET
             persona = excluded.persona, domain = excluded.domain,
             scope_tags = excluded.scope_tags, updated_at = excluded.updated_at""",
        (agent, persona, domain, scope_tags_json, now, now),
    )
    conn.commit()


def get_profile(conn: sqlite3.Connection, agent: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM profiles WHERE agent = ?", (agent,)).fetchone()


def all_profiles(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM profiles ORDER BY agent").fetchall()


def facts_by_tags(conn: sqlite3.Connection, tags: List[str], limit: int) -> List[sqlite3.Row]:
    """Current facts carrying ANY of these tags (the lane channel).

    Tags are stored as a JSON array of normalized kebab strings, so a quoted
    LIKE ('%\"market-size\"%') is an exact tag match, not a substring guess.
    """
    if not tags:
        return []
    clauses = " OR ".join(["tags LIKE ?"] * len(tags))
    params = [f'%"{t}"%' for t in tags]
    return conn.execute(
        f"SELECT * FROM facts WHERE superseded_by IS NULL "
        f"AND validation_status = 'fresh' AND ({clauses}) LIMIT ?",
        (*params, limit),
    ).fetchall()


def facts_by_origin(conn: sqlite3.Connection, origin: str, limit: int) -> List[sqlite3.Row]:
    """Current facts captured by this agent (the provenance channel)."""
    return conn.execute(
        "SELECT * FROM facts WHERE superseded_by IS NULL "
        "AND validation_status = 'fresh' AND origin_tool = ? LIMIT ?",
        (origin, limit),
    ).fetchall()


# ── decision ledger (explainable forgetting) ──────────────────────────────────
def record_decision(conn: sqlite3.Connection, kind: str, rule: str,
                    snippet: str = "") -> None:
    """Every drop/demotion/truncation/fallback is recorded with the rule that
    fired — silent losses were only ever found by benchmarks, months apart
    (loss census 2026-07-05). Rotation runs every ~200th insert (not every
    insert) so the hot capture path stays cheap; the cap is soft by <200 rows."""
    cur = conn.execute(
        "INSERT INTO decisions (ts, kind, rule, snippet) VALUES (?,?,?,?)",
        (now_iso(), kind, rule, (snippet or "")[:160]),
    )
    if cur.lastrowid and cur.lastrowid % 200 == 0:
        conn.execute(
            "DELETE FROM decisions WHERE id <= "
            "(SELECT MAX(id) FROM decisions) - ?",
            (config.LEDGER_CAP,),
        )
    conn.commit()


def recent_decisions(conn: sqlite3.Connection, kind: Optional[str] = None,
                     limit: int = 50) -> List[sqlite3.Row]:
    if kind:
        return conn.execute(
            "SELECT * FROM decisions WHERE kind = ? ORDER BY id DESC LIMIT ?",
            (kind, limit)).fetchall()
    return conn.execute(
        "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def backfill_domain(conn: sqlite3.Connection, origin: str, domain: str) -> int:
    """Stamp the registering agent's domain onto its own NULL-domain captures.

    Pre-profile captures carry domain=NULL, and NULL-domain facts pass every
    agent's domain gate — an unpartitioned residue. Registration closes it for
    the agent's own history (part of the self-healing register step).
    """
    cur = conn.execute(
        "UPDATE facts SET domain = ? WHERE origin_tool = ? AND domain IS NULL",
        (domain, origin),
    )
    conn.commit()
    return cur.rowcount


def get_many(conn: sqlite3.Connection, fids: List[str]) -> dict:
    """Batch fetch by id — the retrieval fuse loop was doing one SELECT per
    candidate, which the store-size-scaled channel depth turned into hundreds
    of round-trips per search."""
    if not fids:
        return {}
    out: dict = {}
    for i in range(0, len(fids), 500):     # stay under SQLite's parameter cap
        chunk = fids[i:i + 500]
        rows = conn.execute(
            f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(chunk))})",
            chunk).fetchall()
        for r in rows:
            out[r["id"]] = r
    return out


def all_current_tags(conn: sqlite3.Connection) -> List[str]:
    """The tags JSON of every current fact (IDF + vocabulary source)."""
    rows = conn.execute(
        "SELECT tags FROM facts WHERE superseded_by IS NULL "
        "AND validation_status = 'fresh'"
    ).fetchall()
    return [r[0] for r in rows]


# ── size control (eviction) ───────────────────────────────────────────────────
def count_facts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]


def count_current(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL AND validation_status = 'fresh'"
    ).fetchone()[0]


def db_size_bytes(conn: sqlite3.Connection) -> int:
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return pages * page_size


def dead_rows(conn: sqlite3.Connection, limit: int) -> List[str]:
    """Rows never served again: superseded, forgotten (invalidated), or expired."""
    rows = conn.execute(
        "SELECT id FROM facts WHERE superseded_by IS NOT NULL "
        "OR validation_status != 'fresh' LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def eviction_candidates(conn: sqlite3.Connection, limit: int) -> List[str]:
    """Least-used current facts first: fewest recalls, oldest touch, lowest confidence."""
    rows = conn.execute(
        "SELECT id FROM facts "
        "WHERE superseded_by IS NULL AND validation_status != 'invalidated' "
        "ORDER BY access_count ASC, COALESCE(last_accessed_at, created_at) ASC, confidence ASC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def hard_delete(conn: sqlite3.Connection, fid: str) -> None:
    """Remove a fact from every table (reclaims space). A tiered store would demote instead."""
    with conn:
        conn.execute("DELETE FROM facts_vec WHERE fact_id = ?", (fid,))
        conn.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fid,))
        conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fid,))
        conn.execute("DELETE FROM facts WHERE id = ?", (fid,))


# ── reads ─────────────────────────────────────────────────────────────────────
def get(conn: sqlite3.Connection, fid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()


def all_current(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM facts WHERE superseded_by IS NULL "
        "AND validation_status = 'fresh' ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def keyword_search(conn: sqlite3.Connection, match_query: str, limit: int) -> List[str]:
    if not match_query:
        return []
    # secondary rowid sort: bm25 ties must break by insertion order, not FTS
    # internals — retrieval order has to be identical when the same transcript
    # is re-ingested into a fresh store (replay determinism)
    rows = conn.execute(
        "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ? "
        "ORDER BY rank, rowid LIMIT ?",
        (match_query, limit),
    ).fetchall()
    return [r[0] for r in rows]


def entity_search(conn: sqlite3.Connection, entities: List[str], limit: int) -> List[str]:
    """Fact ids that mention the query's entities, most overlaps first."""
    if not entities:
        return []
    placeholders = ",".join("?" * len(entities))
    # MIN(rowid) tie-break: equal-overlap facts must order by insertion, not by
    # the GROUP BY's uuid traversal — uuids are random per store, so uuid-order
    # made rankings differ between rebuilds of the SAME transcript (found by
    # the LoCoMo replay-determinism check: 54/60 queries drifted)
    rows = conn.execute(
        f"SELECT fact_id, COUNT(*) AS hits, MIN(rowid) AS first_seen "
        f"FROM fact_entities WHERE entity IN ({placeholders}) "
        f"GROUP BY fact_id ORDER BY hits DESC, first_seen ASC LIMIT ?",
        (*entities, limit),
    ).fetchall()
    return [r[0] for r in rows]


def vector_search(conn: sqlite3.Connection, qvec, limit: int) -> List[str]:
    rows = conn.execute(
        "SELECT fact_id FROM facts_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(qvec), limit),
    ).fetchall()
    return [r[0] for r in rows]


def nearest(conn: sqlite3.Connection, qvec, k: int) -> List[Tuple[str, float]]:
    rows = conn.execute(
        "SELECT fact_id, distance FROM facts_vec WHERE embedding MATCH ? AND k = ? "
        "ORDER BY distance",
        (sqlite_vec.serialize_float32(qvec), k),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO store_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
