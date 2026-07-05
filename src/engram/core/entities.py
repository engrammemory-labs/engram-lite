"""Extract entities — the third retrieval signal (INDEXING_DEEP_DIVE §7).

Multi-signal retrieval fuses *three* scores: semantic (vector), keyword (BM25/FTS), and
**entity** matching. engram-lite previously fused only the first two. Entities are
the proper nouns, names, and identifiers that pin a fact to a specific thing
("Bob", "GitHub Actions", "payments_service", "Q3") — matching on them catches
"everything about X" queries that fuzzy vector search alone can miss.

Locally we extract entities deterministically (no LLM / NER model): capitalized
proper-noun phrases, code-like identifiers, and quoted terms. An NER model or
LLM extractor could fill the same role behind the same contract:
text → list[str] of normalized entities.
"""
from __future__ import annotations

import re
from typing import List

from .. import config

# "Bob", "GitHub Actions", "Q3 Roadmap" — runs of Capitalized words.
_PROPER = re.compile(r"\b[A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*\b")
# "payments_service", "engram.core", "bge-small", "claude-3" — code-like identifiers.
_IDENT = re.compile(r"\b[a-zA-Z][a-zA-Z0-9]*(?:[._-][a-zA-Z0-9]+)+\b")
# "camelCase" names.
_CAMEL = re.compile(r"\b[a-z]+[A-Z][a-zA-Z0-9]*\b")
# anything the writer quoted.
_QUOTED = re.compile(r"[\"'`]([^\"'`\n]{2,40})[\"'`]")

# capitalized words that are almost never entities (sentence-initial / pronouns /
# articles). Folded with the keyword stopwords so "The", "We", "I", … are dropped.
_ENTITY_STOP = {
    "the", "a", "an", "this", "that", "these", "those", "it", "we", "i", "you",
    "he", "she", "they", "my", "our", "your", "his", "her", "their", "but", "and",
    "or", "if", "so", "then", "there", "here", "when", "what", "who", "how", "why",
} | config.STOPWORDS


def extract(text: str) -> List[str]:
    """Return the normalized, de-duplicated entities mentioned in `text`."""
    found: List[str] = []
    for m in _PROPER.finditer(text):
        found.append(m.group(0))
    for m in _IDENT.finditer(text):
        found.append(m.group(0))
    for m in _CAMEL.finditer(text):
        found.append(m.group(0))
    for m in _QUOTED.finditer(text):
        found.append(m.group(1))

    out: List[str] = []
    seen: set[str] = set()
    for raw in found:
        ent = raw.strip().lower()
        if len(ent) < config.ENTITY_MIN_LEN or ent.isdigit() or ent in _ENTITY_STOP:
            continue
        if ent in seen:
            continue
        seen.add(ent)
        out.append(ent)
    return out
