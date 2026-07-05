"""Deterministic tag extraction at write time — no LLM, no network.

A fact's tags are the vocabulary terms found in its text, plus anything the
caller passes explicitly. The vocabulary is the union of every registered
profile's scope_tags plus tags already present in the store — so tagging
improves as profiles are registered, and stays consistent (no synonym drift:
`db` and `database` never fragment if only one is in the vocabulary).

Multi-word tags use lower-kebab form (`connection-pool`) and match their spaced
form in text ("connection pool").
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .. import config


def normalize(tag: str) -> str:
    """Lower-kebab: 'Connection Pool' -> 'connection-pool'."""
    return re.sub(r"[^a-z0-9]+", "-", tag.strip().lower()).strip("-")


# irregular plurals + product-name aliases the suffix rules can't derive.
# Small and explicit on purpose — every entry is a documented equivalence,
# not a guess (census P1: 'children', 'people', 'k8s', 'PostgreSQL' all
# resolved to nothing against child/person/kubernetes/postgres scopes).
_IRREGULAR = {
    "children": "child", "people": "person", "mice": "mouse", "feet": "foot",
    "teeth": "tooth", "geese": "goose", "women": "woman", "men": "man",
}
_ALIASES = {
    "k8s": "kubernetes", "postgresql": "postgres", "js": "javascript",
    "ts": "typescript",
}


def _match_key(tag: str) -> str:
    """Suffix-family key: derivational variants share one key, so
    'market-sizing' ↔ 'market-size', 'deployed' ↔ 'deploy', 'policies' ↔
    'policy', 'deployment' ↔ 'deploy', 'migrations' ↔ 'migration'. Keys are
    only ever compared to other keys (never stored), so aggressive-but-
    consistent stripping is safe. Suffixes strip repeatedly ('migrations' →
    'migration' → 'migr') so noun and verb forms meet at one key; -ment/-ation/
    -ion require 4+ remaining chars so 'station' never becomes 'st'."""
    out = []
    for s in tag.split("-"):
        s = _ALIASES.get(s, _IRREGULAR.get(s, s))
        for _ in range(3):                  # multi-pass: migrations → migration → migr
            if s.endswith("ies") and len(s) > 4:
                s = s[:-3] + "y"
                continue
            stripped = False
            for suf in ("ment", "ation", "tion", "ing", "ed", "es", "s"):
                if s.endswith(suf) and len(s) - len(suf) >= (4 if suf in
                                                             ("ment", "ation", "tion")
                                                             else 3):
                    s = s[: -len(suf)]
                    if len(s) >= 2 and s[-1] == s[-2]:
                        s = s[:-1]          # plann → plan
                    stripped = True
                    break
            if not stripped:
                break
        if s.endswith("e"):
            s = s[:-1]                      # size/sizing both → siz
        out.append(s)
    return "-".join(out)


def _is_subseq(short: str, long: str) -> bool:
    it = iter(long)
    return all(c in it for c in short)


def _abbrev_match(t: str, vocab: List[str]) -> Optional[str]:
    """Resolve abbreviation pairs in either direction (db↔database,
    deploy↔deployment). A pair matches when one term starts the other, or the
    short one is a first-letter-anchored subsequence of the long one (d-b in
    d-a-t-a-b-a-s-e). Only an UNAMBIGUOUS match (exactly one vocabulary
    candidate) is applied — anything else keeps the original tag."""
    if any(c.isdigit() for c in t):
        return None   # dates/versions ('august-4th', 'v2-3-1') are never abbreviations
    candidates = []
    for v in vocab:
        short, long = (t, v) if len(t) <= len(v) else (v, t)
        if len(short) < 2 or short[0] != long[0]:
            continue
        if long.startswith(short) or (len(short) <= 4 and _is_subseq(short, long)):
            candidates.append(v)
    return candidates[0] if len(candidates) == 1 else None


def _vocab_index(vocabulary: Iterable[str]):
    """Shared preprocessing for both write (extract) and repair (canonicalize)
    paths so they resolve tags identically."""
    vocab = sorted({normalize(v) for v in vocabulary if normalize(v)})
    vset = set(vocab)
    families: dict = {}
    for v in vocab:
        families.setdefault(_match_key(v), []).append(v)
    return vocab, vset, families


def _resolve(t: str, vocab: List[str], vset: set, families: dict,
             allow_abbrev: bool = True) -> str:
    """Map one normalized token onto its vocabulary term, or return it
    unchanged if it has no vocabulary relative.

    This is the single matching brain shared by canonicalize() (which KEEPS
    unresolved tokens — they may be new concepts) and extract() (which DROPS
    them — the write path only tags known vocabulary). Resolution order:
    exact → singular/plural → suffix-family → abbreviation/prefix.

    allow_abbrev gates the loosest rule (prefix/subsequence, db↔database). It is
    safe for canonicalize(), whose inputs are a handful of DELIBERATE tags, but
    unsafe over free text where every common word is a candidate ('to'→'tone',
    'secondary'→'seo'). extract()'s token pass therefore turns it OFF and relies
    only on the morphologically-sound plural/suffix-family rules.
    """
    if t in vset:
        return t
    if t.endswith("s") and t[:-1] in vset:
        return t[:-1]
    if t.endswith("es") and t[:-2] in vset:
        return t[:-2]                       # statuses → status, patches → patch
    if t + "s" in vset:
        return t + "s"
    fam = families.get(_match_key(t))
    if fam and len(set(fam)) == 1:
        return fam[0]                       # suffix family: sizing → size
    if allow_abbrev:
        return _abbrev_match(t, vocab) or t
    return t


def canonicalize(tag_list: Optional[List[str]], vocabulary: Iterable[str]) -> List[str]:
    """Map tags onto the KNOWN vocabulary so synonyms don't fragment lanes.

    Model-supplied tags drift ('database' vs the scope's 'db', 'migration' vs
    'migrations') and drifted tags silently break lane matching. Resolution
    order: exact match → singular/plural → abbreviation/prefix (db↔database,
    deploy↔deployment). Tags with no vocabulary relative are kept as-is (they
    may be genuinely new concepts).
    """
    vocab, vset, families = _vocab_index(vocabulary)
    out: List[str] = []
    for raw in tag_list or []:
        t = normalize(raw)
        if not t:
            continue
        t = _resolve(t, vocab, vset, families)
        if t not in out:
            out.append(t)
    return out


def extract(
    text: str,
    vocabulary: Iterable[str],
    explicit: Optional[List[str]] = None,
    cap: int = config.TAGS_PER_FACT_CAP,
) -> List[str]:
    """Tags for one fact: explicit tags first, then vocabulary terms found in
    the text — matched the SAME way canonicalize() repairs drifted tags.

    A literal pass catches multi-word tags ('connection pool'); a morphological
    pass then resolves single-token variants ('query'→'queries',
    'endpoint'→'endpoints', 'database'→'db') so the write path tags exactly what
    the repair path would recognize. Without this, an unqualified capture whose
    wording differs by a suffix from the scope term lands untagged and can never
    be served in-lane.
    """
    vocab, vset, families = _vocab_index(vocabulary)

    tags: List[str] = []
    for t in explicit or []:
        n = normalize(t)
        if n and n not in tags:
            tags.append(n)

    # Two views of the text: as-written, and with intra-word hyphens collapsed
    # so hyphenated spellings find their joined vocabulary term ("on-call
    # rotation" matches scope tag `oncall`; "e-mail" matches `email`).
    lower = text.lower()
    joined = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", lower)
    views = [" " + re.sub(r"[^a-z0-9]+", " ", v) + " " for v in (lower, joined)]

    # 1) literal vocabulary terms present verbatim (handles multi-word tags)
    for term in vocab:
        if len(tags) >= cap:
            break
        spaced = " " + term.replace("-", " ") + " "
        if term not in tags and any(spaced in v for v in views):
            tags.append(term)

    # 2) morphological matches on individual tokens, resolved through the same
    #    brain as canonicalize() but WITHOUT the loose abbreviation rule — over
    #    free text only the plural/suffix-family rules are precise enough
    #    (query→queries, endpoint→endpoints, sizing→size).
    if len(tags) < cap:
        tokens = re.findall(r"[a-z0-9]+", lower) + re.findall(r"[a-z0-9]+", joined)
        for tok in tokens:
            if len(tags) >= cap:
                break
            r = _resolve(tok, vocab, vset, families, allow_abbrev=False)
            if r in vset and r not in tags:
                tags.append(r)
    return tags[:cap]
