"""Split one interaction into several atomic memory candidates.

The write path's single biggest quality lever is *fact extraction*: one message usually
carries several distinct, reusable facts ("I moved to Zed, and we dropped the ORM
for pgx" is two), and storing it as one blob makes both consolidation and
retrieval coarse. Locally we have no LLM, so we approximate extraction with
deterministic clause splitting — break on sentence boundaries and a few *safe*
clause separators, then let the salience gate (and consolidation) judge each
candidate on its own.

The extraction seam is deliberate: an LLM extractor can later
be slotted in behind `extract()` without touching any caller. The contract is
simply text → list[str] of candidate facts.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from .. import config
from . import anchors

# Hard sentence/clause boundaries that almost always separate independent facts:
# sentence terminators, semicolons, bullets, em/en dashes, and newlines.
# A period directly after a digit is NOT a boundary — that's a numbered list
# ("1. screen 2. coding") or a version ("upgrade to 1.31"), and splitting there
# produced junk fragments like "Freeze deploys 3.".
# Abbreviation guard (loss census P0): "Dr. Nguyen" and "400 W. Georgia St." must
# not sever — a single capital letter or a title/street abbreviation before the
# period is part of a name, not a sentence end.
_HARD_SPLIT = re.compile(
    r"(?<=[^\d][.!?])"
    r"(?<!\b[A-Z]\.)(?<!Dr\.)(?<!Mr\.)(?<!Mrs\.)(?<!Ms\.)(?<!St\.)(?<!Ave\.)"
    r"(?<!Blvd\.)(?<!Prof\.)(?<!Jr\.)(?<!Sr\.)(?<!vs\.)(?<!e\.g\.)(?<!i\.e\.)(?<!etc\.)"
    r"\s+|(?<=[!?])\s+|\s*[;•·]\s*|\s+[—–]\s+|\n+"
)

# A conservative conjunction split: ", and " / ", but " join two clauses far more
# often than a noun phrase ("Bob and Alice" has no comma), so we only split there.
_CONJ_SPLIT = re.compile(r",\s+(?:and|but|and then|and also|whereas|however)\s+", re.IGNORECASE)

_MIN_PART = 8   # conjunction-split halves below this are a wrong split — keep whole
_MIN_FRAG = 4   # fragments shorter than this (chars) are debris
_MIN_WORDS = 2  # "New job!" / "I quit." are legitimate terse facts (census P1/P2);
                # the per-fragment salience gate still judges what survives here


def _split_conjunctions(chunk: str) -> List[str]:
    """Split a chunk on ', and'-style joins, but only when both sides are substantial."""
    pieces = _CONJ_SPLIT.split(chunk)
    if len(pieces) == 1:
        return [chunk]
    # if any side is too small, the split was probably wrong — keep the chunk whole
    if any(len(p.strip()) < _MIN_PART for p in pieces):
        return [chunk]
    return [p.strip() for p in pieces]


def extract_full(text: str,
                 max_facts: int = config.MAX_FACTS_PER_INTERACTION) -> Tuple[List[str], bool]:
    """Break `text` into atomic candidate facts.

    Returns (candidates, truncated). Candidates keep their original order and
    are de-duplicated case-insensitively; at least one element (the whole text)
    is always returned for non-empty input.

    When more candidates exist than `max_facts`, the cut keeps the most
    INFORMATIVE ones (anchor-bearing — names/numbers/negators — then longer)
    rather than the first N by position: a 10-sentence message's only two
    informative facts used to be silently dropped because they came last
    (loss census P1). `truncated` tells the caller to surface the cut.
    """
    text = " ".join(text.split())
    if not text:
        return [], False

    parts: List[str] = []
    for chunk in _HARD_SPLIT.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.extend(_split_conjunctions(chunk))

    seen: set[str] = set()
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if len(p) < _MIN_FRAG or len(p.split()) < _MIN_WORDS:
            continue  # too small to stand alone as a fact
        norm = p.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(p)

    truncated = len(out) > max_facts
    if truncated:
        ranked = sorted(out, key=lambda p: (not anchors.anchor_tokens(p), -len(p)))
        keep = set(ranked[:max_facts])
        out = [p for p in out if p in keep]   # cut by informativeness, keep order

    return (out or [text]), truncated


def extract(text: str, max_facts: int = config.MAX_FACTS_PER_INTERACTION) -> List[str]:
    """Compatibility wrapper: candidates only (see extract_full)."""
    return extract_full(text, max_facts)[0]
