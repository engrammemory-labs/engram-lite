"""The "is this new or already known?" decision (MEMORY_LIFECYCLE §5).

Given a new fact's vector + key and its block, look at the nearest existing fact
*in that block* and classify the write into a four-way operation set:

    REINFORCE  near-identical (≥ REINFORCE_SIM)              → bump confidence, no new row
    DELETE     same topic but the new fact NEGATES the old   → retire the stale belief, add new
    UPDATE     same topic, refined/changed value             → supersede the old version
    ADD        nothing close enough                          → brand-new fact

(The "already known, do nothing" case is often called NOOP; here it becomes REINFORCE
so repetition strengthens a memory instead of being silently dropped.)

Pure decision logic — it does not write; `memory.py` applies the result via the
repository. Contradiction is detected deterministically (negation polarity); a
semantic LLM resolver could be slotted in behind this same contract.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from .. import config
from ..storage import repository
from . import anchors

# words/markers that flip a statement's polarity ("Bob no longer owns payments").
# Motion/completion verbs (dropped/stopped/ended/removed) are NOT here: "Bob
# dropped the kids at school" is an event, not a negation — with them in the
# list it invalidated Bob's standing routine (loss census 2026-07-05).
_NEGATIONS = (
    "not", "no longer", "never", "isn't", "aren't", "wasn't", "weren't", "don't",
    "doesn't", "didn't", "won't", "can't", "cannot",
    "deprecated", "cancelled", "canceled", "discontinued", "no more",
)
_NEG_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in _NEGATIONS) + r")\b", re.IGNORECASE)


def _cosine_from_l2(distance: float) -> float:
    """For unit vectors, euclidean^2 = 2 - 2cos  ⇒  cos = 1 - d^2/2."""
    return 1.0 - (distance * distance) / 2.0


def _has_negation(text: str) -> bool:
    return bool(_NEG_RE.search(text or ""))


def _contradicts(old_key: str, new_key: str) -> bool:
    """True when one statement negates the other (opposite polarity)."""
    return _has_negation(old_key) != _has_negation(new_key)


def _nearest_in_block(conn, block_id: str, vec) -> Optional[Tuple[str, float, str]]:
    for fid, dist in repository.nearest(conn, vec, k=10):
        row = repository.get(conn, fid)
        if row is None or row["superseded_by"] is not None:
            continue
        if row["validation_status"] != "fresh":
            continue
        if row["block_id"] != block_id:
            continue
        return fid, _cosine_from_l2(dist), row["key"]
    return None


def decide(conn, block_id: str, vec, key: str = "") -> Tuple[str, Optional[str], str]:
    """Return (operation, target_fact_id, note). target is None for ADD.

    `note` is non-empty when a merge was DEMOTED by the anchor guard — the
    caller records it in the decision ledger so demotions are auditable.
    """
    nearest = _nearest_in_block(conn, block_id, vec)
    if nearest is None:
        return "ADD", None, ""
    fid, sim, old_key = nearest

    if sim >= config.UPDATE_SIM:
        # anchor guard: if each statement carries an identity token the other
        # lacks (orders vs refunds, Tuesday vs Thursday), these are two TRUE
        # sibling facts however similar the wording — merging/retiring either
        # one destroys real memory. Keep both. (Loss census 2026-07-05.)
        if anchors.exclusive_anchors(old_key, key):
            return "ADD", None, f"merge demoted: exclusive anchors vs {fid} (cos {sim:.2f})"
        # same topic: does the new statement contradict (negate) the old one?
        if _contradicts(old_key, key):
            return "DELETE", fid, ""
        if sim >= config.REINFORCE_SIM:
            return "REINFORCE", fid, ""
        return "UPDATE", fid, ""

    return "ADD", None, ""
