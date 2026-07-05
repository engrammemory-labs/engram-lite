"""The embedder contract: anything with `.dim` and `.embed(text) -> list[float]`."""
from __future__ import annotations

import math
from typing import List, Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> List[float]: ...


def normalize(vec: List[float]) -> List[float]:
    """Scale to unit length so L2 distance lines up with cosine similarity."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]
