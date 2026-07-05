"""Deterministic relative-date resolution at capture time.

People state facts in relative time — "I went last Friday", "we're moving next
month" — and the absolute date exists only in the conversation's own timestamp.
LLM-extraction systems resolve this during ingestion with a model call;
we resolve it with calendar arithmetic: zero LLM, same information.

`resolve_relatives(text, anchor)` annotates each relative expression inline
with its computed absolute form: "I went last Friday [= Friday, 14 July 2023]".
Inline (not appended) so keyword/vector retrieval keeps the date next to the
event it dates. Text inside existing [= ...] annotations is never re-annotated.

Measured motivation (LoCoMo track B): 58% of temporal failures had the
evidence SERVED but relative — the answering model can't do the arithmetic
reliably; the store should carry it resolved.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

_MONTHS = "january february march april may june july august september october november december".split()
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_WORD_NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple of": 2,
              "few": 3}

_ANCHOR_PATTERNS = [
    "%d %B, %Y", "%d %B %Y", "%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%d/%m/%Y",
]


def parse_anchor(s: Optional[str]) -> Optional[_dt.date]:
    """Parse a conversation timestamp like '8 May, 2023' into a date."""
    if not s:
        return None
    s = s.strip()
    # tolerate a leading time ("1:56 pm on 8 May, 2023")
    if " on " in s:
        s = s.split(" on ")[-1].strip()
    for fmt in _ANCHOR_PATTERNS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return _dt.date(int(m.group(3)), _MONTHS.index(m.group(2).lower()) + 1,
                        int(m.group(1)))
    return None


def _fmt_date(d: _dt.date) -> str:
    return f"{_WEEKDAYS[d.weekday()].capitalize()}, {d.day} {_MONTHS[d.month - 1].capitalize()} {d.year}"


def _fmt_month(d: _dt.date) -> str:
    return f"{_MONTHS[d.month - 1].capitalize()} {d.year}"


def _month_shift(d: _dt.date, months: int) -> _dt.date:
    m = d.month - 1 + months
    return _dt.date(d.year + m // 12, m % 12 + 1, 1)


def resolve_relatives(text: str, anchor: Optional[_dt.date],
                      max_annotations: int = 3) -> str:
    """Annotate relative time expressions with computed absolutes, inline."""
    if anchor is None or "[=" in text:
        return text
    count = 0

    def ann(match_text: str, resolved: str) -> str:
        nonlocal count
        count += 1
        return f"{match_text} [= {resolved}]"

    def sub(pattern: str, resolver) -> None:
        nonlocal text
        def repl(m: re.Match) -> str:
            if count >= max_annotations:
                return m.group(0)
            r = resolver(m)
            return ann(m.group(0), r) if r else m.group(0)
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    # exact-day expressions
    sub(r"\byesterday\b", lambda m: _fmt_date(anchor - _dt.timedelta(days=1)))
    sub(r"\blast night\b", lambda m: _fmt_date(anchor - _dt.timedelta(days=1)))
    sub(r"\b(today|tonight|this morning|this evening|this afternoon)\b",
        lambda m: _fmt_date(anchor))
    sub(r"\btomorrow\b", lambda m: _fmt_date(anchor + _dt.timedelta(days=1)))
    # last/next <weekday>: the most recent such day strictly before (after) anchor
    sub(r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lambda m: _fmt_date(anchor - _dt.timedelta(
            days=((anchor.weekday() - _WEEKDAYS.index(m.group(1).lower())) % 7) or 7)))
    sub(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lambda m: _fmt_date(anchor + _dt.timedelta(
            days=((_WEEKDAYS.index(m.group(1).lower()) - anchor.weekday()) % 7) or 7)))
    # periods — phrased the way LoCoMo golds phrase them
    sub(r"\blast\s+week(end)?\b",
        lambda m: f"the week{'end' if m.group(1) else ''} before {anchor.day} "
                  f"{_MONTHS[anchor.month - 1].capitalize()} {anchor.year}")
    sub(r"\bthis\s+week(end)?\b",
        lambda m: f"the week{'end' if m.group(1) else ''} of {anchor.day} "
                  f"{_MONTHS[anchor.month - 1].capitalize()} {anchor.year}")
    sub(r"\blast\s+month\b", lambda m: _fmt_month(_month_shift(anchor, -1)))
    sub(r"\bnext\s+month\b", lambda m: _fmt_month(_month_shift(anchor, 1)))
    sub(r"\bthis\s+month\b", lambda m: _fmt_month(anchor))
    sub(r"\blast\s+year\b", lambda m: str(anchor.year - 1))
    sub(r"\bnext\s+year\b", lambda m: str(anchor.year + 1))
    # "N days/weeks/months/years ago"
    def ago(m: re.Match) -> Optional[str]:
        raw = m.group(1).lower()
        n = _WORD_NUMS.get(raw) or (int(raw) if raw.isdigit() else None)
        if n is None:
            return None
        unit = m.group(2).lower()
        if unit == "day":
            return _fmt_date(anchor - _dt.timedelta(days=n))
        if unit == "week":
            return _fmt_date(anchor - _dt.timedelta(weeks=n))
        if unit == "month":
            return _fmt_month(_month_shift(anchor, -n))
        return str(anchor.year - n)
    sub(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|few|couple of)"
        r"\s+(day|week|month|year)s?\s+ago\b", ago)
    return text
