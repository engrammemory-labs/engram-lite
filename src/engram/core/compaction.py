"""Keep each memory small, so it costs little to store and few tokens to return.

  - `compact_key`  → a short one-line label (the thing we embed + the handle).
  - `cap_value`    → the full text, trimmed to a guardrail length.

This is the cheap, offline version of "summarize the memory." A real LLM summary
could fill the same role (MEMORY_LIFECYCLE §2/§6); here we just normalize + trim.
"""
from __future__ import annotations

import re

from .. import config
from . import anchors

# first-sentence boundary with the SAME digit + abbreviation guards extraction
# uses: without them "Priorities: 1." and "Dr." became the embedded key and the
# consolidation handle — matching nothing at recall and colliding across every
# "Dr. X" fact (loss census P1)
_FIRST_SENTENCE = re.compile(
    r"(?<=[^\d][.!?])"
    r"(?<!\b[A-Z]\.)(?<!Dr\.)(?<!Mr\.)(?<!Mrs\.)(?<!Ms\.)(?<!St\.)(?<!Ave\.)"
    r"(?<!Blvd\.)(?<!Prof\.)(?<!Jr\.)(?<!Sr\.)(?<!vs\.)(?<!e\.g\.)(?<!i\.e\.)(?<!etc\.)"
    r"\s"
)


def compact_key(text: str, cap: int = config.KEY_CHAR_CAP) -> str:
    """A short, clean one-liner: collapse whitespace, take the first sentence, trim.

    When the cap truncates, anchor tokens (names/numbers) from the cut tail are
    appended — they're what makes the fact findable, and losing them made
    semantic match on the cut content impossible (loss census P1).
    """
    t = " ".join(text.split())
    first = _FIRST_SENTENCE.split(t, maxsplit=1)[0]
    if len(first) <= cap:
        return first
    head = t[:cap].rsplit(" ", 1)[0]
    lost = anchors.anchor_tokens(t) - anchors.anchor_tokens(head)
    if lost:
        head = f"{head} {' '.join(sorted(lost)[:4])}"[: cap + 48]
    return head


def cap_value(text: str, cap: int = config.VALUE_CHAR_CAP) -> str:
    """The full memory text, capped so a stray long paste can't bloat the store."""
    t = text.strip()
    if len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0] + " …"
