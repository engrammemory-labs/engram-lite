"""Open the SQLite file, load sqlite-vec, apply the schema.

WAL mode + a busy timeout are set so multiple processes (e.g. Claude and Cursor
each running their own server against the same file) can read/write concurrently
without tripping over locks. `check_same_thread=False` lets one connection be
used from more than one thread — frameworks like Hermes dispatch memory writes
on a background worker thread while reads happen on the main thread. SQLite's
serialized mode makes that safe; the Memory engine additionally serializes all
access behind a lock.
"""
from __future__ import annotations

import sqlite3
import time

import sqlite_vec

from . import schema


def _open_once(path: str, dim: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        # concurrency hardening for multi-process access. busy_timeout FIRST:
        # the WAL switch itself needs a write lock, so two processes doing
        # their first open simultaneously race on it.
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        # WAL + NORMAL is the canonical local-app pairing: commits stop paying
        # an fsync each (the decision ledger writes often), integrity is
        # unaffected — at worst a power cut loses the last moments of writes,
        # it can never corrupt the store.
        conn.execute("PRAGMA synchronous=NORMAL;")
        # sqlite-vec is a loadable extension: enable, register, disable
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        schema.init_schema(conn, dim)
        return conn
    except BaseException:
        conn.close()   # a failed open must not hold locks while we retry
        raise


def connect(path: str, dim: int) -> sqlite3.Connection:
    """Open with bounded retry on first-open contention.

    busy_timeout does NOT cover every collision: SQLite returns BUSY
    immediately (no wait) on read→write lock upgrades to avoid deadlock, which
    two processes creating the same store simultaneously can hit inside the
    schema DDL (red-team round 2: 'database is locked' crashes on simultaneous
    first open). One side finishes; the other just needs to try again against
    the now-existing schema, where every statement no-ops.
    """
    last: Exception | None = None
    for attempt in range(12):
        try:
            return _open_once(path, dim)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if not ("locked" in msg or "busy" in msg or "already exists" in msg):
                raise
            last = e
            time.sleep(0.05 * (attempt + 1))
    raise last  # type: ignore[misc]
