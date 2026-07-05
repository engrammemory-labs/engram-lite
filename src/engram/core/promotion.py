"""Conditioned promotion — serve each agent ITS memory, not the same pile.

This is the piece that makes memory *serving* different from memory *search*.
Plain retrieval answers "what matches the query?" and gives every agent the same
answer. Promotion answers "what should THIS agent see?" by conditioning on the
agent's profile (persona / domain / scope) and the task in front of it.

The algorithm is the "lane model", validated on CAMP-Bench, an internal
benchmark (two engineering domains, 30 cases each; see the README for caveats). Measured against human-authored gold: precision 0.22 →
0.61+, recall 0.59 → 0.86+, profile-discrimination 0.00 → 0.90, correct
abstention 9/9. Downstream, with a live agent: task success 40% → 80% and
hallucination cut to a third; a flat shared pile made the agent hallucinate MORE
than no memory at all.

How it works, in one breath:

    lane = task_tags ∩ profile.scope_tags     # what this situation is about
                                              # that this agent actually owns
    empty lane            -> serve NOTHING (correct abstention)
    otherwise             -> score facts by IDF-weighted overlap with the lane,
                             gate out other domains, apply a relevance floor,
                             serve at most k (fewer when little truly fits)

Deterministic, microseconds, no LLM. Every promoted fact carries `why` (the lane
tags it hit and its score) so serving decisions are explainable.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

from .. import config

_STOPWORDS = {
    "the", "is", "a", "an", "of", "it", "to", "and", "from", "at", "in", "on",
    "we", "have", "for", "next", "this", "that", "our", "has", "was", "are",
}


def _words(text: str) -> set:
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in raw if len(w) >= 3 and w not in _STOPWORDS}


def word_overlap(a: str, b: str) -> int:
    """How many content words two texts share (tiny tie-break signal)."""
    return len(_words(a) & _words(b))


def idf_weights(tag_lists: List[List[str]]) -> Dict[str, float]:
    """Rare tags carry more signal than ubiquitous ones. Classic IDF over the store."""
    n = max(len(tag_lists), 1)
    df: Dict[str, int] = {}
    for tags in tag_lists:
        for t in set(tags):
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1.0) / (c + 0.5)) for t, c in df.items()}


def promote(
    candidates: List[dict],
    profile: dict,
    task_tags: List[str],
    query: str,
    k: int,
    idf: Optional[Dict[str, float]] = None,
    floor_frac: float = config.PROMOTION_FLOOR_FRAC,
) -> List[dict]:
    """Rank access-filtered candidates for ONE agent. May return fewer than k, or [].

    candidates: fact dicts with `tags` (list) and optional `domain`.
    profile:    {"persona": str, "domain": str, "scope_tags": [str, ...]}.
    Returns the served facts, each with a `promotion` dict attached:
        {"score": float, "lane": [tags hit], "mode": "lane-v1"}.
    """
    lane = set(task_tags) & set(profile.get("scope_tags") or [])
    if not lane:
        return []  # this agent has no stake in this situation -> abstain

    if idf is None:
        idf = idf_weights([f.get("tags") or [] for f in candidates])

    agent_label = profile.get("agent")
    agent_domain = profile.get("domain")
    scored: List[tuple] = []
    own: List[tuple] = []
    for f in candidates:
        # hard domain partition: never serve another discipline's memory
        if agent_domain and f.get("domain") and f["domain"] != agent_domain:
            continue
        hit = set(f.get("tags") or []) & lane
        if not hit:
            # provenance channel: a fact THIS agent captured belongs to its
            # lane even when vocabulary tagging found no in-scope word in the
            # text (the deterministic tagger's ceiling). Own facts never
            # displace lane hits — they only fill slots the lane left empty.
            if agent_label and f.get("origin_tool") == agent_label:
                own.append((word_overlap(f.get("value", ""), query),
                            f.get("created_at") or "", f))
            continue  # must connect to the situation THROUGH a tag this agent owns
        score = sum(idf.get(t, 0.0) for t in hit)
        score += config.PROMOTION_EPSILON * word_overlap(f.get("value", ""), query)
        scored.append((score, sorted(hit), f))

    served: List[dict] = []
    spill: List[tuple] = []   # own lane facts the floor or k cut — spare-slot eligible
    if scored:
        # ties break on CONTENT (created_at sequence, then key) — never on the
        # uuid, which is random per store: uuid ties made rebuilt stores serve
        # different boot snapshots (5/5 rebuilds differed, loss census — the
        # replay-determinism guarantee must hold across re-ingestion)
        scored.sort(key=lambda x: (-x[0], x[2].get("created_at") or "",
                                   x[2].get("key") or ""))
        top = scored[0][0]
        for score, hit, f in scored:
            if len(served) < k and score >= floor_frac * top:
                out = dict(f)
                out["promotion"] = {"score": round(score, 4), "lane": hit, "mode": "lane-v1"}
                served.append(out)
            elif agent_label and f.get("origin_tool") == agent_label:
                # the floor keeps weak matches from padding a SHARED lane, but an
                # agent's own capture is never "padding" to that agent — it stays
                # eligible for slots the lane leaves empty (provenance channel).
                spill.append((score, hit, f))

    if len(served) < k and (spill or own):
        seen = {s.get("id") for s in served}
        # floor-cut own lane facts first (they carry lane relevance, already
        # sorted by score), then vocabulary-untagged own facts by query overlap.
        for score, hit, f in spill:
            if len(served) >= k:
                break
            if f.get("id") in seen:
                continue
            seen.add(f.get("id"))
            out = dict(f)
            out["promotion"] = {"score": round(score, 4), "lane": hit, "mode": "origin-v1",
                                "reason": "own capture, below lane floor"}
            served.append(out)
        own.sort(key=lambda t: t[1], reverse=True)   # fresher first
        own.sort(key=lambda t: t[0], reverse=True)   # query overlap dominates (stable)
        for overlap, _, f in own:
            if len(served) >= k:
                break
            if f.get("id") in seen:
                continue
            seen.add(f.get("id"))
            out = dict(f)
            out["promotion"] = {"score": overlap, "lane": [], "mode": "origin-v1",
                                "reason": "own capture, vocabulary-untagged"}
            served.append(out)
    return served
