"""Pluggable embeddings: local model by default, hash stub as fallback.

ENGRAM_EMBEDDER=hash forces the stub (no model download — used by CI and quick
trials); ENGRAM_EMBEDDER=local forces the real model. Unset = try local, fall
back to the stub.
"""
from __future__ import annotations

import os
import sys

from .base import Embedder, normalize
from .hashing import HashEmbedder
from .local import LocalEmbedder, model_cache_dir, model_name

__all__ = ["Embedder", "normalize", "HashEmbedder", "LocalEmbedder", "get_embedder"]


def _looks_uncached() -> bool:
    """Has the local model been downloaded to our stable cache yet? (times a notice)."""
    import glob
    tail = model_name().split("/")[-1]
    return not glob.glob(os.path.join(model_cache_dir(), "**", f"*{tail}*"),
                         recursive=True)


def _local_embedder() -> "LocalEmbedder":
    # First run pulls the model (one-time), then it's fully offline. Announce
    # on stderr so a fresh install never looks like a hang; silent once cached.
    if _looks_uncached():
        print(f"[engram] downloading local embedding model {model_name()} "
              "(one-time) — then fully offline…", file=sys.stderr, flush=True)
    return LocalEmbedder()


def get_embedder(prefer_local: bool = True) -> Embedder:
    """Return the best available embedder, falling back to the hash stub."""
    forced = os.environ.get("ENGRAM_EMBEDDER", "").strip().lower()
    if forced == "hash":
        return HashEmbedder()
    if forced == "local":
        return _local_embedder()
    if prefer_local:
        try:
            return _local_embedder()
        except Exception as exc:  # noqa: BLE001 — any import/model failure → stub
            # stderr, never stdout: a host tool may own stdout as a protocol channel
            print(f"[engram] local model unavailable ({exc}); using hash stub.",
                  file=sys.stderr)
    return HashEmbedder()
