"""Keep the store under a size limit by removing the least-useful memories.

When the DB grows past `max_facts`:
  1. first hard-delete dead rows (already superseded or forgotten) — pure dead weight;
  2. then, if still over, hard-delete the least-used *current* facts
     (lowest access_count, then oldest-touched, then lowest confidence).

Locally we hard-delete to actually reclaim space. (A tiered store could instead
*demote* cold memories to a cheaper tier — see STORAGE_AND_RETRIEVAL §5/§6.3.)
"""
from __future__ import annotations

from ..storage import repository


def enforce(conn, max_facts: int) -> int:
    """Remove memories until the DB is within `max_facts`. Returns how many removed."""
    total = repository.count_facts(conn)
    over = total - max_facts
    if over <= 0:
        return 0

    removed = 0
    # phase 1: dead rows (superseded / invalidated)
    for fid in repository.dead_rows(conn, limit=over):
        repository.hard_delete(conn, fid)
        removed += 1
    over -= removed

    # phase 2: least-used current rows
    if over > 0:
        for fid in repository.eviction_candidates(conn, limit=over):
            repository.hard_delete(conn, fid)
            removed += 1

    return removed
