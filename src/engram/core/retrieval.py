"""The read funnel: keyword + vector + entity → RRF → relevance floor → recency.

(INDEXING_DEEP_DIVE §7). Three signals are fused:

  - keyword  (FTS5, exact word match after dropping stopwords)
  - vector   (sqlite-vec k-NN, semantic similarity)
  - entity   (proper-noun / identifier overlap — "everything about X")

A candidate is kept only if it earned its place in *some* channel (a keyword hit,
an entity hit, or vector similarity above a floor) — so we never return weak
memories just to fill the top-k. Survivors are ranked by their fused RRF score
tilted slightly toward fresher memories (recency weighting).

`memory.py` calls this with `touch=False` when it over-fetches candidates for
conditioned promotion, so only the facts actually served count as "accessed"
for eviction.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import List, Optional

from .. import config
from ..storage import repository
from . import anchors, entities, rrf


def _cosine_from_l2(distance: float) -> float:
    """For unit vectors, euclidean^2 = 2 - 2cos  ⇒  cos = 1 - d^2/2."""
    return 1.0 - (distance * distance) / 2.0


def _fts_query(text: str) -> str:
    """Free text → FTS5 OR-query of meaningful word tokens (stopwords dropped)."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in config.STOPWORDS]
    return " OR ".join(tokens)


def _recency_factor(created_at: str, now: _dt.datetime) -> float:
    """A mild multiplier in ((1-RECENCY_WEIGHT), 1] — fresher memories rank higher."""
    if not config.RECENCY_WEIGHT:
        return 1.0
    try:
        created = _dt.datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return 1.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=_dt.timezone.utc)
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    freshness = 0.5 ** (age_days / config.RECENCY_HALFLIFE_DAYS)
    return (1.0 - config.RECENCY_WEIGHT) + config.RECENCY_WEIGHT * freshness


def _near_dup(a: str, b: str) -> bool:
    """Token-set Jaccard — cheap paraphrase detector for serve-time dedup."""
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.6


def collapse_near_dups(rows: List[dict], limit: int | None = None) -> List[dict]:
    """Drop later results that paraphrase an earlier one (double-capture echoes).

    Serving three phrasings of the same fact wastes top-k slots; keep the
    highest-ranked phrasing only. Write-path fixes reduce dupes at the source;
    this is the serve-time backstop.

    Anchor guard: two facts that each carry an identity token the other lacks
    ("Bob owns payments" / "Bob owns refunds") are SIBLINGS, not paraphrases —
    Jaccard alone collapsed 8/12 such pairs and hid true answers (loss census
    P0). Siblings are never collapsed.

    `limit` stops after that many survivors: rows arrive ranked, callers serve
    at most k, and collapsing the whole store-size-scaled candidate list was
    O(n²) — 327k Jaccard comparisons across 20 searches. Token sets are
    computed once per row; the anchor guard only runs on actual near-dups.
    """
    kept: List[dict] = []
    kept_toks: List[set] = []
    for r in rows:
        if limit is not None and len(kept) >= limit:
            break
        toks = set(re.findall(r"[a-z0-9]+", (r["value"] or "").lower()))
        dup = False
        for kt, kr in zip(kept_toks, kept):
            if toks and kt and len(toks & kt) / len(toks | kt) >= 0.6 \
                    and not anchors.exclusive_anchors(r["value"], kr["value"]):
                dup = True
                break
        if not dup:
            kept.append(r)
            kept_toks.append(toks)
    return kept


def search(conn, embedder, query: str, block_id: Optional[str] = None,
           k: int = config.DEFAULT_TOP_K, validate: bool = True,
           touch: bool = True) -> List[dict]:
    qvec = embedder.embed(query)
    n = max(config.CANDIDATES_PER_CHANNEL, k)
    # NOTE on channel depth (measured, LoCoMo ablation 2026-07-05): scaling n
    # with store size was tried to fix mid-rank facts being unreachable at
    # ~1000+ facts, and it REGRESSED recall@10 by 6.6 points — deep pools feed
    # RRF's multi-channel-mediocrity bias (a generic fact at rank ~80 in all
    # three channels outscores a fact at rank 5 in one). Deepening the pull
    # needs rank-windowed fusion first; until then, fixed depth wins.

    keyword_ids = repository.keyword_search(conn, _fts_query(query), n)
    near = repository.nearest(conn, qvec, n)                       # [(fid, distance)]
    query_entities = entities.extract(query)
    entity_ids = repository.entity_search(conn, query_entities, n) if query_entities else []

    # multi-entity questions ("How long after adopting Biscuit did Caroline
    # move?") need evidence about EACH entity, but a single fused query lets
    # the dominant entity crowd the pool — 80% of multi-hop failures were
    # evidence-absent (LoCoMo decomposition). One extra keyword pull per
    # entity unions each entity's strongest facts into the candidate pool.
    if len(query_entities) >= 2:
        for ent in sorted(query_entities)[:4]:
            for fid in repository.keyword_search(conn, _fts_query(ent), n // 2):
                if fid not in keyword_ids:
                    keyword_ids.append(fid)

    # relevance floor: a fact qualifies if it matched on keywords or entities, or
    # its vector similarity clears the threshold — otherwise it's noise, drop it.
    # The floor is a SEMANTIC threshold: it only means something on a calibrated
    # embedding model. The hash-stub's cosines are arbitrary, so with it every
    # vector candidate qualifies (pre-floor behavior) rather than none.
    # Known tradeoff (accepted): keyword hits qualify unconditionally, and the
    # porter stemmer can produce wrong-stem matches ('university' ↔ 'universe').
    # Those rank at the RRF bottom and only surface when nothing better matched;
    # gating them on embedding similarity would also kill the RIGHT-stem matches
    # ('experiments' → 'experiment') that porter exists to catch.
    floor_applies = getattr(embedder, "calibrated", True)
    qualified = set(keyword_ids) | set(entity_ids)
    vector_ids: List[str] = []
    for fid, dist in near:
        vector_ids.append(fid)
        if not floor_applies or _cosine_from_l2(dist) >= config.MIN_SIMILARITY:
            qualified.add(fid)

    scores = rrf.fuse_scores([keyword_ids, vector_ids, entity_ids])

    today = repository.today_iso()
    now = _dt.datetime.now(_dt.timezone.utc)
    rows_by_id = repository.get_many(conn, [f for f in scores if f in qualified])
    ranked: List[tuple] = []
    for fid, base in scores.items():
        if fid not in qualified:
            continue
        row = rows_by_id.get(fid)
        if row is None:
            continue
        if row["superseded_by"] is not None:
            continue
        if row["validation_status"] != "fresh":
            continue  # invalidated (forgotten/contradicted) or expired
        if validate and row["valid_until"] and row["valid_until"] < today:
            continue  # expired but not yet swept
        if block_id and row["block_id"] != block_id:
            continue
        final = base * _recency_factor(row["created_at"], now)
        if query_entities:
            # entity-anchored rerank: a fact that names the entities the
            # question names should outrank a generically-similar one. The
            # answer-extraction failure class was evidence sitting deep in the
            # served list while generic matches filled the top (decomposition:
            # 17% of failures). Multiplicative, small, deterministic.
            shared = sum(1 for e in query_entities
                         if e.lower() in (row["value"] or "").lower())
            if shared:
                final *= 1.0 + config.ENTITY_RERANK_BONUS * min(shared, 3)
        ranked.append((final, row))

    # equal fused scores tie-break on CONTENT (created_at sequence, then key),
    # never on uuid or dict order — rankings must be identical when the same
    # transcript is re-ingested into a fresh store (replay determinism)
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["created_at"], pair[1]["key"]))
    results = collapse_near_dups([dict(row) for _, row in ranked], limit=k)

    if touch:
        # record usage on the facts we actually returned (drives eviction recency)
        for r in results:
            repository.touch_access(conn, r["id"])
    return results
