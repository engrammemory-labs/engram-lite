"""Anchor tokens — the parts of a fact that make it THAT fact.

"orders p99 is 450ms" and "refunds p99 is 450ms" share almost every word, but
each carries a content token the other lacks. Any merge / collapse / invalidate
decision between two such texts is a guess, and losing a true fact is worse
than keeping a near-sibling.

The guard is only consulted AFTER a caller has established near-identity
(consolidation: cosine ≥ 0.86 on the same block; serve-time collapse:
Jaccard ≥ 0.6) — inside a frame that similar, a differing content stem is not
paraphrase variance, it is the identity of a different fact. One-sided
differences (only one text has a negator, or extra detail) do NOT trigger:
that's the genuine update/negation case the write path exists to handle.

Loss census (2026-07-05) receipts: the UPDATE band silently superseded sibling
facts at cos 0.86-0.95, DELETE false-fired on motion verbs, and serve-time
near-dup collapse hid 8/12 distinct sibling pairs.
"""
from __future__ import annotations

import re
from typing import Set

# words that flip a statement's polarity — genuine negators only. Motion /
# completion verbs ("dropped the kids at school", "the meeting ended") are NOT
# negation; they false-fired the DELETE path (census) and live nowhere here.
NEGATORS = {
    "not", "no", "never", "isn't", "aren't", "wasn't", "weren't", "don't",
    "doesn't", "didn't", "won't", "can't", "cannot", "longer",     # "no longer"
    "cancelled", "canceled", "discontinued", "deprecated",
}

_STOP = {
    "the", "a", "an", "of", "to", "and", "or", "for", "with", "on", "in", "at",
    "is", "are", "was", "were", "has", "have", "had", "be", "been", "its",
    "it's", "this", "that", "these", "those", "every", "each", "all", "some",
    "we", "our", "they", "their", "he", "she", "his", "her", "you", "your",
    "i", "my", "me", "now", "then", "also", "very", "just", "will", "would",
}

_TOKEN = re.compile(r"[A-Za-z0-9'’\-]+")


def _stem(tok: str) -> str:
    """Cheap derivational stem so 'deploys'/'deploying'/'deployed' agree.
    Only used for anchor comparison — never stored."""
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            tok = tok[: -len(suf)]
            if len(tok) >= 2 and tok[-1] == tok[-2]:
                tok = tok[:-1]
            break
    return tok[:-1] if tok.endswith("e") else tok


def _strong_weak(text: str) -> tuple[Set[str], Set[str]]:
    """STRONG anchors are definitive identity tokens: digits ("450ms",
    "Tuesday at 10am"'s 10am), negators, and mid-sentence capitalized words
    (names, days, products). WEAK anchors are the remaining content stems."""
    strong: Set[str] = set()
    weak: Set[str] = set()
    for i, tok in enumerate(_TOKEN.findall(text or "")):
        low = tok.lower()
        if any(c.isdigit() for c in tok):
            strong.add(low)
        elif low in NEGATORS:
            strong.add(low)
        elif i > 0 and tok[0].isupper():
            strong.add(_stem(low))
        elif len(low) >= 3 and low not in _STOP:
            # drop apostrophes BEFORE stemming: "lot's" → lots → lot (a
            # trailing-strip left "lot'" and the filler floor never matched)
            weak.add(_stem(low.replace("'", "").replace("’", "")))
    return strong, weak


def anchor_tokens(text: str) -> Set[str]:
    """All identity-bearing tokens of a fact (strong ∪ weak)."""
    strong, weak = _strong_weak(text)
    return strong | weak


def exclusive_anchors(a: str, b: str) -> bool:
    """True when the two near-identical texts are DIFFERENT facts.

    Two signals, both deterministic:
      1. strong anchors differ on both sides — "payments deploys Tuesday" vs
         "... Thursday", "p99 450ms" vs a different number owner, one side
         negated per anchor asymmetry;
      2. a 1-for-1 content-stem substitution in an otherwise shared frame —
         "Bob owns payments" vs "Bob owns refunds". Paraphrases differ by
         ADDITIVE wording (several extra stems on one or both sides), never by
         a clean substitution, so genuine rewordings still merge/collapse.
    """
    sa, wa = _strong_weak(a)
    sb, wb = _strong_weak(b)
    if (sa - sb) and (sb - sa):
        return True
    ea = wa - wb - sb
    eb = wb - wa - sa
    return len(ea) == 1 and len(eb) == 1
