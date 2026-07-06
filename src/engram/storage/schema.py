"""The database schema, in one place.

Keeping the DDL here (separate from connection logic) makes it easy to evolve and,
later, to add migrations. This is the concrete form of INDEXING_DEEP_DIVE.md §6.1.

v2 adds conditioned promotion + the entity signal:
  - facts.tags       JSON array of lower-kebab tags (what the fact is about)
  - facts.domain     the writing agent's discipline (hard partition at promotion time)
  - profiles         one row per agent on this machine: persona / domain / scope_tags
  - fact_entities    proper nouns / identifiers per fact (the third retrieval signal)
"""
from __future__ import annotations

import sqlite3

# the record of truth + its B-tree indexes + the FTS5 keyword index
FACTS_AND_FTS = """
CREATE TABLE IF NOT EXISTS facts (
    id                TEXT PRIMARY KEY,           -- uuid (B-tree on the PK)
    block_id          TEXT NOT NULL,             -- narrowing key, e.g. 'person:bob'
    subject_key       TEXT NOT NULL,
    key               TEXT NOT NULL,             -- short label: keyword-searched + embedded
    value             TEXT NOT NULL,             -- full text returned to the caller
    fragment_type     TEXT NOT NULL DEFAULT 'fact',
    confidence        REAL NOT NULL DEFAULT 1.0,
    created_at        TEXT NOT NULL,             -- ISO-8601
    valid_until       TEXT,                      -- nullable hard expiry
    volatility_class  TEXT NOT NULL DEFAULT 'static',
    validation_status TEXT NOT NULL DEFAULT 'fresh',
    access_count      INTEGER NOT NULL DEFAULT 0,  -- how often recalled (for eviction)
    last_accessed_at  TEXT,                         -- when last recalled (for eviction)
    origin_tool       TEXT,                         -- which AI tool wrote it (provenance)
    prior_version_id  TEXT,                         -- version chain (UPDATE)
    superseded_by     TEXT,                         -- NULL = current version
    tags              TEXT NOT NULL DEFAULT '[]',   -- JSON array: what this fact is about
    domain            TEXT                          -- writer's discipline (promotion partition)
);

CREATE INDEX IF NOT EXISTS idx_facts_block   ON facts(block_id);
CREATE INDEX IF NOT EXISTS idx_facts_valid   ON facts(valid_until);
CREATE INDEX IF NOT EXISTS idx_facts_created ON facts(created_at);
CREATE INDEX IF NOT EXISTS idx_facts_current ON facts(block_id)
    WHERE superseded_by IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED,
    key,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id TEXT NOT NULL,
    entity  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity);
CREATE INDEX IF NOT EXISTS idx_fact_entities_fact   ON fact_entities(fact_id);

CREATE TABLE IF NOT EXISTS profiles (
    agent      TEXT PRIMARY KEY,   -- the agent label (matches facts.origin_tool)
    persona    TEXT NOT NULL,      -- who the agent is, e.g. 'on-call SRE'
    domain     TEXT NOT NULL,      -- its discipline, e.g. 'sre-devops'
    scope_tags TEXT NOT NULL,      -- JSON array: the concepts this agent owns
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,   -- store-level settings that must survive process churn
    value TEXT NOT NULL       -- e.g. max_facts: the cap is a property of the STORE,
                              -- not of whichever process happens to open it
);

CREATE TABLE IF NOT EXISTS decisions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- capture-skip | fragment-skip | truncation |
                             -- merge-demote | serve-fallback | serve-abstain
    rule    TEXT NOT NULL,   -- the rule that fired (explainable forgetting)
    snippet TEXT             -- head of the affected input, for diagnosis
);
"""

# columns added after v1 — applied to pre-existing DBs on open (idempotent)
_MIGRATIONS = [
    ("tags", "ALTER TABLE facts ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"),
    ("domain", "ALTER TABLE facts ADD COLUMN domain TEXT"),
]


def vec_table_sql(dim: int) -> str:
    """The sqlite-vec virtual table (vectors). Dimension comes from the embedder."""
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0("
        f"fact_id TEXT PRIMARY KEY, embedding FLOAT[{dim}]);"
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-v2 facts table up to date. No-op on fresh DBs."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
    for col, ddl in _MIGRATIONS:
        if col not in existing:
            conn.execute(ddl)


def _migrate_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the keyword index with the porter stemmer if it predates it.

    FTS5 has no stemming by default, so 'experiments' never matched a fact
    keyed 'experiment' — a real recall miss observed live. The index is derived
    data (facts.key is the source of truth), so drop-and-repopulate is safe;
    fresh DBs get porter from the DDL and skip this entirely.

    The guard is POPULATION-aware, not just DDL-aware: a crash between DROP and
    repopulate leaves a porter-tokenized but EMPTY index, and a DDL-only check
    would skip it forever — silently killing the keyword channel for every
    pre-existing fact (red-team round 2). Every fact has exactly one facts_fts
    row (insert_fact adds it, hard-delete removes it), so a rowcount mismatch
    means the index diverged and gets rebuilt — self-healing on next open.
    """
    if conn.execute("SELECT name FROM sqlite_master WHERE name = 'facts'"
                    ).fetchone() is None:
        return  # fresh DB: the DDL creates everything correctly
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'facts_fts'").fetchone()
    if row is not None and "porter" not in (row[0] or ""):
        conn.execute("DROP TABLE facts_fts")
        row = None
    if row is None:
        # IF NOT EXISTS: two processes can race past the sqlite_master check
        # during a simultaneous first open — the loser must no-op, not crash
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
            "fact_id UNINDEXED, key, tokenize = 'porter unicode61')")
    # BEGIN IMMEDIATE serializes the check-and-rebuild across processes: the
    # counts are re-read under the write lock, so two concurrent healers can't
    # interleave DELETE/INSERT into a duplicated index.
    conn.execute("BEGIN IMMEDIATE")
    try:
        n_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM facts_fts").fetchone()[0]
        if n_fts != n_facts:
            conn.execute("DELETE FROM facts_fts")
            conn.execute(
                "INSERT INTO facts_fts (fact_id, key) SELECT id, key FROM facts")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def existing_vec_dim(conn: sqlite3.Connection) -> int | None:
    """The dimension the store's vector table was created with, if it exists."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'facts_vec'").fetchone()
    if not row or not row[0]:
        return None
    m = __import__("re").search(r"FLOAT\[(\d+)\]", row[0], __import__("re").IGNORECASE)
    return int(m.group(1)) if m else None


def init_schema(conn: sqlite3.Connection, dim: int) -> None:
    _migrate_fts(conn)          # BEFORE the DDL: IF NOT EXISTS must not keep a stale index
    existing = existing_vec_dim(conn)
    if existing is not None and existing != dim:
        # a store is bound to its embedding model's dimension: silently mixing
        # 384-d rows with a 768-d query would corrupt retrieval — fail with
        # the fix instead
        raise ValueError(
            f"this store's vectors are {existing}-dim but the configured "
            f"embedder produces {dim}-dim. Rebuild once with "
            f"Memory.reembed(path) (or `engram rebuild <db>`), or set "
            f"ENGRAM_EMBEDDER_MODEL back to the model this store was built with.")
    conn.executescript(FACTS_AND_FTS)
    _migrate(conn)
    conn.execute(vec_table_sql(dim))
    conn.commit()
