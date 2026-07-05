"""A deterministic, dependency-free placeholder embedder.

Hashes word tokens into buckets. Good enough to exercise the schema, FTS5,
sqlite-vec, and the search funnel — NOT good enough for real semantic recall.
Used as the fallback when the local model isn't installed, and in tests.
"""
from __future__ import annotations

import hashlib
import re
from typing import List

from .. import config
from .base import normalize


class HashEmbedder:
    # token-hash cosines are not comparable to a real model's — retrieval must
    # not apply its semantic similarity floor (MIN_SIMILARITY) to these vectors
    calibrated = False

    def __init__(self, dim: int = config.FALLBACK_DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return normalize(vec)
