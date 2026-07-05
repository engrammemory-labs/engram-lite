"""Strip secrets before anything is written to memory.

A memory store that auto-writes must never quietly persist an API key or token a
user pasted. `scrub()` replaces detected secrets with `<redacted:KIND>` markers and
reports what it found, so the surrounding fact is kept but the secret never lands in
the DB. Heuristic (offline) — covers the common shapes; not a full DLP scanner.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# order matters: more specific patterns first (anthropic before the generic sk- key)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # (?=…\d…\d) — a real key body has digit entropy; without it the sk- rule
    # unrecoverably ate ML slugs like 'sk-learn-tutorial' and
    # 's3://models/sk-distilbert-finetuned-v2-final' (loss census P1)
    ("anthropic_key", re.compile(r"\bsk-ant-(?=[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?(?=[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
# "api_key=...", "password: ...", "token = ..." — redact the value, keep the label
_KV_SECRET = re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b(\s*[:=]\s*)[\"']?[^\s\"']{8,}")


def scrub(text: str) -> Tuple[str, List[str]]:
    """Return (clean_text, kinds_found). clean_text has secrets replaced with markers."""
    found: List[str] = []
    clean = text
    for kind, pat in _PATTERNS:
        if pat.search(clean):
            found.append(kind)
            clean = pat.sub(f"<redacted:{kind}>", clean)

    def _kv(m: re.Match) -> str:
        found.append("generic_secret")
        return f"{m.group(1)}{m.group(2)}<redacted:secret>"

    clean = _KV_SECRET.sub(_kv, clean)
    return clean, list(dict.fromkeys(found))   # de-duplicated, order preserved
