"""Resolve a memory to a subject/block label (the narrowing key).

Getting this right at scale is real entity-resolution work (lifecycle doc Q1);
locally we keep it deliberately simple and naive — a placeholder to improve later.
"""
from __future__ import annotations

import re

_CAP_WORD = re.compile(r"\b([A-Z][a-zA-Z]{2,})\b")


def resolve(text: str, subject: str | None = None) -> str:
    if subject:
        key = subject.strip().lower()
        return key if ":" in key else f"topic:{key}"
    m = _CAP_WORD.search(text)
    if m:
        return f"subject:{m.group(1).lower()}"
    return "misc"
