"""Decide whether a piece of text is worth remembering — and roughly what kind.

This is the gate that keeps junk out of memory. The guiding question is:
*"Would this be useful to recall in a future, different conversation?"*

  SAVE   durable facts: "Bob owns payments", "we decided to use pgx not an ORM",
         "I have a meeting at 5:30am", a preference, a fact looked up online.
  SKIP   transient/process output: code edits, file dumps, command output,
         greetings, and bare questions.

Locally this is a fast heuristic (no LLM). It's deliberately conservative about
code/output, since that's the most common thing you do NOT want in memory. A
smarter LLM-based classifier could replace it behind the same interface (see
MEMORY_LIFECYCLE §2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from . import anchors

_GREETINGS = {"hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "bye", "yes", "no"}

# lines that look like code/commands/diffs. Bare '@' removed: it matched
# "@raj owns the payments database now" (loss census) — decorators still die
# on symbol density.
_CODE_LINE = re.compile(
    r"^\s*(import |from \w+ import|def |class |function |const |let |var |public |private |"
    r"#include|\}|\{|</?\w+>|\$ |npm |pip |git |sudo |cd |\+\+\+|---|@@)"
)
# ()[] removed from the density alphabet: rosters, schedules, and scores are
# paren-heavy human notes ("Raj (payments), Sam (auth)") — census P0.
_SYMBOLS = re.compile(r"[{};=<>]")

# fenced code spans — stripped before whole-message judgment so a 24-char
# inline snippet can never veto the prose around it (census P0)
_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove ```fenced``` spans; the surrounding prose is judged on its own."""
    return _FENCE.sub(" ", text)


@dataclass(frozen=True)
class Salience:
    keep: bool
    reason: str
    fragment_type: str = "fact"
    volatility_class: str = "static"


def _looks_like_code(text: str) -> bool:
    if "```" in text:                       # fenced code block
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    codey = sum(1 for ln in lines if _CODE_LINE.match(ln))
    if len(lines) >= 2 and codey / len(lines) >= 0.4:
        return True                          # lots of code-shaped lines
    if len(lines) == 1 and codey:
        # a single prose line opening with let/class/git/cd is usually English
        # ("let me know when Raj lands") — require symbol corroboration before
        # calling one line code (loss census P0)
        return len(_SYMBOLS.findall(text)) >= 3
    if len(text) > 40:                       # dense in code symbols
        if len(_SYMBOLS.findall(text)) / len(text) >= 0.06:
            return True
    return False


def _classify(text: str) -> tuple[str, str]:
    low = text.lower()
    if re.search(r"\b\d{1,2}:\d{2}\s?(am|pm)?\b|\b(tomorrow|today|tonight|deadline|due|meeting|appointment)\b", low):
        return "task", "time_bounded"
    if re.search(r"\b(we (decided|chose|agreed)|let'?s use|going with|use \w+ not \w+)\b", low):
        return "decision", "slow"
    if re.search(r"\b(i (prefer|like|hate|always|never)|always |never )\b", low):
        return "opinion", "static"
    return "fact", "static"


# meta-chatter and bare imperatives — the openers of fragments that are ABOUT
# remembering rather than things worth remembering ("Note the plan down.",
# "Recorded.", "I'll track this.", "Ready to execute when you are.").
# "I'll", "we'll", "this is" only count as meta when completed by a
# bookkeeping verb/state — "I'll be in Paris next week" is a durable fact and
# must never match (red-team round 2: the broad openers silently dropped real
# facts from multi-fact messages).
_META_OPENER = re.compile(
    r"^(note[ds]?|remember|record(ed)?|done|got it|stored|logged|okay|ok|sure|ready|"
    r"next steps?|key considerations?|action required|"
    r"(i'?ll|we'?ll)\s+(track|note|record|remember|log|handle|monitor|watch|"
    r"follow|get back|take care|circle back|make a note|keep (an eye|that|this|it))|"
    r"keep (it|this|that)\s+(in mind|handy|safe|around|somewhere)|"
    r"keep (it|this|that)[.!]?$|"
    r"(this is|that'?s) (done|it|all|ready|correct|right|fine|noted|handled|"
    r"complete|everything|good))\b[.:!,]?",
    re.IGNORECASE,
)
# NOTE: 'recommend(ed)' was removed from the opener list — "Recommended dosage
# is 200mg twice daily" is a durable fact (loss census P0).

# conversational filler stems: a fragment whose content is ONLY these words
# ("A lot's happened since we last chatted", "It was such a fun day") has
# declarative SHAPE but no durable content. Storing them floods retrieval —
# they are semantically near every query, so real facts get outranked.
# Compared against stemmed content tokens (anchors._strong_weak weak set).
_FILLER_STEMS = frozenset({
    "happen", "chat", "chatt", "talk", "catch", "spoke", "speak", "lot",
    "thing", "stuff", "time", "day", "week", "month", "lately", "recently",
    "nice", "great", "good", "lovely", "fun", "amazing", "awesome",
    "wonderful", "glad", "happy", "excited", "excit", "busy", "crazy", "wild",
    "going", "update", "new", "sinc", "last", "hear", "see", "seen", "know",
    "hope", "feel", "felt", "way", "bit", "much", "many", "real", "realli",
    "such", "still", "yet", "even", "well", "back", "here", "there",
    # reactive exclamations — "Me too!", "So cool!", "Sounds great!" gained
    # entry when the 2-word fragment floor opened for terse facts; they are
    # reactions, not facts
    "too", "cool", "wow", "yeah", "yep", "nope", "haha", "lol", "omg",
    "same", "right", "agre", "total", "definit", "absolut", "exact",
    "sweet", "neat", "perfect", "congrat", "sound", "look", "seem",
})
# code that survived extraction's whitespace collapse ("def retry(fn): return ...")
_INLINE_CODE = re.compile(r"\b(def|return|import|class|const|let|var|lambda)\b\s|\(\)|=>|::")
# corroboration alphabet for the inline-code check: parens INCLUDED here (the
# code keyword is the trigger; parens corroborate) but EXCLUDED from _SYMBOLS
# density (where they killed paren-heavy human notes — rosters, schedules)
_CODE_SYMBOLS = re.compile(r"[{}();=<>\[\]]")

# terminal/process artifacts that arrive as "user" turns when a chat runs in a
# shell (observed live: "received signal 1" captured as a memory), plus bare
# slash-commands. Runtime noise, never facts.
_TERMINAL_ARTIFACT = re.compile(
    r"^(received signal \d+|signal \d+ received|\^[a-z]|/[a-z][a-z-]*|"
    r"segmentation fault.*|killed:?\s*\d*|terminated|exit(ed)?\s+(code|status)\s+\d+|"
    r".*command not found|broken pipe|eof)$",
    re.IGNORECASE,
)


def is_terminal_artifact(text: str) -> bool:
    """Shell/runtime noise masquerading as a turn — reject before anything else."""
    t = text.strip().rstrip(".!,;:")   # 'Terminated.' is as junky as 'Terminated'
    return bool(_TERMINAL_ARTIFACT.match(t)) or "\x1b[" in text


def is_noise_fragment(text: str) -> bool:
    """Per-CANDIDATE check applied after extraction splits a message.

    The whole-text gate can miss these: short imperative/meta fragments, and
    code whose newlines extraction collapsed away. Deliberately aggressive on
    short fragments only — anything 9+ words passes through to `assess`.
    """
    t = text.strip()
    if is_terminal_artifact(t):
        return True
    words = t.split()
    m = _META_OPENER.match(t)
    if m:
        # judge the REMAINDER, not just the opener: "Keep this in mind: I'm
        # allergic to peanuts" carries a real fact after the meta phrase — the
        # old whole-fragment match dropped it (loss census P0, 14/16
        # ack-then-fact messages lost). Two conditions separate ack-then-fact
        # from pure meta: (a) a punctuation boundary after the opener (":"/"."
        # — a NEW utterance, not the same clause continuing: "I'll track this
        # for release day" has none), and (b) the remainder standing alone as
        # a non-noise fact (recursion is bounded — the remainder is strictly
        # shorter, so "Noted. I'll track this." stays dead).
        tail = t[m.end():]
        rest = tail.lstrip(" .:!,;—-")
        boundary = tail[: len(tail) - len(rest)]
        matched_end = t[:m.end()].rstrip()[-1:]
        has_break = any(c in ".:!;—" for c in boundary) or matched_end in ".:!;"
        if (has_break and len(rest.split()) >= 3
                and assess(rest).keep and not is_noise_fragment(rest)):
            return False
        if len(words) <= 8:
            return True
    if _INLINE_CODE.search(t) and len(_CODE_SYMBOLS.findall(t)) >= 4:
        return True
    return False


def assess(text: str) -> Salience:
    t = text.strip()
    if len(t) < 4:
        return Salience(False, "too short to be a useful memory")
    if len(t) < 8 and len(t.split()) < 2 and not any(c.isdigit() for c in t):
        # the 8-char floor sat on the terse-life-event cliff ("I quit.",
        # "I'm 42.", "Use pgx" — loss census P1): two words or a digit is
        # enough shape to be a fact; a lone short word still isn't.
        return Salience(False, "too short to be a useful memory")
    if is_terminal_artifact(t):
        return Salience(False, "terminal/process artifact, not a fact")
    if t.lower() in _GREETINGS:
        return Salience(False, "greeting / acknowledgement")
    if len(t) > config.MAX_TEXT_CHARS:
        return Salience(False, "too long — looks like a file or output dump")
    if _looks_like_code(t):
        return Salience(False, "looks like code or command output")
    if t.endswith("?"):
        return Salience(False, "a question, not a fact to remember")
    strong, weak = anchors._strong_weak(t)
    if not strong and weak and weak <= _FILLER_STEMS:
        # declarative shape, zero durable content — "A lot's happened since we
        # last chatted." Storing these floods retrieval: they sit semantically
        # near EVERY query and outrank real facts (measured on LoCoMo: rescued
        # filler cost -6.6 recall@10 before this floor).
        return Salience(False, "conversational filler, no durable content")
    ftype, vol = _classify(t)
    return Salience(True, "durable fact", ftype, vol)
