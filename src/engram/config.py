"""Build-time knobs — constants the engine is tuned with.

Distinct from `settings.py`, which reads *runtime* flags from environment variables.
"""
from __future__ import annotations

# ── storage ──────────────────────────────────────────────────────────────────
DEFAULT_DB_PATH = "engram.db"   # one SQLite file holds everything (STORAGE_AND_RETRIEVAL §3)

# ── embeddings ───────────────────────────────────────────────────────────────
EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # small, local, offline model (fastembed)
FALLBACK_DIM = 384                       # dim of the hash-stub embedder + bge-small
# Pin the model cache to a STABLE dir so it downloads once and stays offline —
# fastembed's default is $TMPDIR, which the OS wipes (silent re-downloads).
# Override with ENGRAM_MODEL_CACHE.
MODEL_CACHE_DIR = "~/.cache/engram/models"

# ── consolidation thresholds (cosine similarity, 0..1) ───────────────────────
# Four-way operation set: ADD / UPDATE / DELETE / NOOP — here NOOP is upgraded
# to REINFORCE (bump confidence instead of silently dropping the duplicate).
REINFORCE_SIM = 0.97   # ~identical to an existing fact → just bump it, no new row
UPDATE_SIM = 0.86      # close enough to be the same fact → refine, contradict, or supersede

# ── extraction (one interaction → several atomic facts) ──────────────────────
# The write path's biggest quality lever: split a message into discrete, reusable facts
# instead of storing it as one blob (MEMORY_LIFECYCLE §2).
MAX_FACTS_PER_INTERACTION = 8  # cap on candidates pulled from a single remember() call

# ── retrieval ────────────────────────────────────────────────────────────────
CANDIDATES_PER_CHANNEL = 30   # how many to pull from keyword, vector, and entity search
RRF_K = 60                     # Reciprocal Rank Fusion constant (INDEXING_DEEP_DIVE §7)
DEFAULT_TOP_K = 5              # how many memories to return by default
SEARCH_K_CAP = 200            # k is clamped to [1, this]: a mistyped/hostile k must
                              # neither blow the knn query limit nor blackout serving
LEDGER_CAP = 2000             # decision-ledger rows kept (capped rotation)
ENTITY_RERANK_BONUS = 0.15    # multiplicative boost per query entity a fact names
                              # (max 3); tuned on LoCoMo dev convs 26+30 ONLY

# framework-stamped pseudo-tags that carry no task information — treating them
# as a lane caused a total silent serving blackout (loss census P1)
GENERIC_TASK_TAGS = frozenset({
    "conversation", "chat", "general", "message", "misc", "context", "default",
    "session", "dialogue", "turn",
})
MIN_SIMILARITY = 0.30         # vector candidates below this cosine are dropped (noise floor)

# recency: at equal relevance, fresher memories rank higher (INDEXING_DEEP_DIVE §7).
# final_score = rrf_score × ((1 - RECENCY_WEIGHT) + RECENCY_WEIGHT × freshness),
# where freshness = 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS) ∈ (0, 1].
RECENCY_WEIGHT = 0.30          # how much recency may tilt the ranking (0 = off)
RECENCY_HALFLIFE_DAYS = 30.0   # a memory this old contributes half its freshness

# ── entities (the third retrieval signal) ────────────────────────────────────
ENTITY_MIN_LEN = 2            # ignore entity tokens shorter than this

# common words ignored in the keyword (FTS) query so it doesn't match everything
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "in", "on",
    "for", "and", "or", "i", "you", "we", "it", "this", "that", "what", "who",
    "whom", "how", "why", "when", "where", "should", "would", "could", "my",
    "your", "our", "here", "there", "do", "does", "did", "use", "used", "can",
    "will", "with", "about", "from", "at", "as", "by", "me", "s",
}

# ── conditioned promotion (the lane model — see core/promotion.py) ───────────
PROMOTION_FLOOR_FRAC = 0.34   # keep a fact only if score >= this fraction of the top score
PROMOTION_EPSILON = 0.05      # weight of the word-overlap tie-break signal
PROMOTION_OVERFETCH = 4       # promotion re-ranks OVERFETCH×k retrieval candidates
PROMOTION_MIN_CANDIDATES = 30 # ...but never fewer than this many candidates
LANE_FETCH_LIMIT = 100        # lane channel: max lane-tagged facts added as candidates
TAGS_PER_FACT_CAP = 8         # max tags stored per fact

# ── salience (what to save) + compaction (how compactly) ─────────────────────
KEY_CHAR_CAP = 160            # the short embedded label is trimmed to this
VALUE_CHAR_CAP = 1000         # the stored value is capped (guardrail against blobs)
MAX_TEXT_CHARS = 2000         # longer than this → assumed file/output dump, skip saving

# ── size limit / eviction ────────────────────────────────────────────────────
MAX_FACTS = 5000              # per DB; least-used facts are evicted beyond this
