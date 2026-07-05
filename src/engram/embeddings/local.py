"""A small, offline embedding model via fastembed (default: bge-small-en-v1.5).

The model cache is pinned to a STABLE directory (`config.MODEL_CACHE_DIR`, or the
`ENGRAM_MODEL_CACHE` env override) so it downloads exactly once. fastembed's own
default is the OS temp dir, which gets wiped — causing silent re-downloads and
breaking the "offline after first run" promise.
"""
from __future__ import annotations

import os
from typing import List

from .. import config
from .base import normalize


def model_cache_dir() -> str:
    """Stable, persistent location for the downloaded embedding model."""
    d = os.environ.get("ENGRAM_MODEL_CACHE") or config.MODEL_CACHE_DIR
    d = os.path.expanduser(d)
    os.makedirs(d, exist_ok=True)
    return d


def model_name() -> str:
    """Which local embedding model to load. ENGRAM_EMBEDDER_MODEL overrides
    the default (e.g. BAAI/bge-base-en-v1.5: 768-dim, ~215MB, measurably
    better retrieval than bge-small). A store is bound to its model's
    dimension — switching models on an existing store needs Memory.reembed();
    the open guard explains this rather than corrupting anything."""
    return os.environ.get("ENGRAM_EMBEDDER_MODEL", "").strip() or config.EMBED_MODEL


class LocalEmbedder:
    def __init__(self, model: str | None = None) -> None:
        from fastembed import TextEmbedding  # imported lazily so the stub works without it

        self.model_name = model or model_name()
        self._model = TextEmbedding(model_name=self.model_name,
                                    cache_dir=model_cache_dir())
        probe = next(iter(self._model.embed(["dimension probe"])))
        self.dim = len(probe)

    def embed(self, text: str) -> List[float]:
        vec = next(iter(self._model.embed([text]))).tolist()
        return normalize(vec)
