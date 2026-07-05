"""Reciprocal Rank Fusion — merge keyword + vector + entity result lists into one.

Cormack et al. (2009): a result ranking high in *several* lists floats to the top.
See INDEXING_DEEP_DIVE.md §7.
"""
from __future__ import annotations

from typing import Dict, List

from .. import config


def fuse_scores(ranked_lists: List[List[str]], k0: int = config.RRF_K) -> Dict[str, float]:
    """Per-id fused score: each list contributes 1/(k0 + rank + 1)."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k0 + rank + 1)
    return scores


def fuse(ranked_lists: List[List[str]], k0: int = config.RRF_K) -> List[str]:
    """Ids ordered by descending fused score (thin wrapper over `fuse_scores`)."""
    scores = fuse_scores(ranked_lists, k0)
    return sorted(scores, key=lambda i: scores[i], reverse=True)
