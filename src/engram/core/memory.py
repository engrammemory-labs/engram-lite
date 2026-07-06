"""The Memory engine — the orchestrator for Save / Find / Update / Forget.

Wires together: embeddings (text → vector), storage (the SQLite repository), and
the core logic (subject resolution, redaction, extraction, the consolidation
decision, the retrieval funnel, and conditioned promotion). This is the public
face of the engine; everything else is a detail it delegates to.

The write path: scrub secrets → gate junk → split one interaction
into atomic facts → consolidate each (ADD / UPDATE / DELETE / REINFORCE).

The read path: three-signal retrieval (keyword + vector + entity, with a noise
floor and recency weighting) — and, for agents with a registered *profile*
(persona / domain / scope_tags), a conditioned promotion pass on top: each agent
is served the memory that fits ITS lane, may get fewer than k results, and gets
nothing (correct abstention) when the situation is outside its scope. Agents
without a profile get plain multi-signal search, unchanged.
"""
from __future__ import annotations

import functools
import json
import os
import threading
import uuid
from typing import List, Optional

from .. import config
from ..embeddings import Embedder, get_embedder
from ..storage import db, repository
from . import (
    compaction,
    dates,
    consolidation,
    entities,
    eviction,
    extraction,
    promotion,
    redaction,
    retrieval,
    salience,
    subjects,
)
from . import tags as tagging


def _tag_list(value) -> Optional[List[str]]:
    """Tolerate a comma-string where a tag list is expected.

    A bare string iterates as CHARACTERS: tags='hiring' would store
    ['h','i','r','n','g'] and permanently pollute the shared vocabulary with
    single letters, while task_tags='hiring' would falsely abstain (red-team
    round 2). Models and callers make this type mistake constantly — absorb it.
    """
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        return [t for t in (p.strip() for p in value.split(",")) if t]
    return [str(value)]


# whole-message gate reasons that are FATAL before extraction: no fragment of
# such a message could survive on its own. Everything else ("a question...",
# "too long...") is judged fragment-by-fragment — a rule may kill a sentence,
# never a turn (loss census 2026-07-05).
_FATAL_WHOLE_MESSAGE = {
    "too short to be a useful memory",
    "greeting / acknowledgement",
    "terminal/process artifact, not a fact",
    "looks like code or command output",
}


def _locked(fn):
    """Serialize a public method behind the instance lock.

    Every operation touches the one SQLite connection — including "reads",
    which write access counts — and a host framework may call read (main
    thread) and write (background thread) concurrently. A re-entrant lock makes
    all of it safe; nested calls (remember → save → search) re-acquire freely.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Memory:
    """A local, file-backed memory store. One instance = one SQLite file."""

    def __init__(self, path: str = config.DEFAULT_DB_PATH,
                 embedder: Optional[Embedder] = None, origin_tool: Optional[str] = None,
                 max_facts: Optional[int] = None):
        self.embedder = embedder or get_embedder()
        self.origin_tool = origin_tool          # provenance: which tool/agent is writing
        self._lock = threading.RLock()          # serialize all connection access
        self.conn = db.connect(path, dim=self.embedder.dim)
        # The eviction cap is a property of the STORE, persisted in store_meta:
        # a store seeded at a 25k ceiling must never be mass-evicted because a
        # later process opened it without ENGRAM_MAX_FACTS in its environment.
        # Precedence: explicit arg > env (records a new ceiling) > the store's
        # own recorded cap > the build default.
        stored = repository.get_meta(self.conn, "max_facts")
        if max_facts is not None:
            self.max_facts = max_facts
        elif "ENGRAM_MAX_FACTS" in os.environ:
            self.max_facts = int(os.environ["ENGRAM_MAX_FACTS"])
        elif stored is not None:
            self.max_facts = int(stored)
        else:
            self.max_facts = config.MAX_FACTS
        if stored is None or int(stored) != self.max_facts:
            repository.set_meta(self.conn, "max_facts", str(self.max_facts))

    # ── PROFILES (conditioned promotion) ─────────────────────────────────────
    @_locked
    def register_profile(self, agent: str, persona: str, domain: str,
                         scope_tags: List[str]) -> dict:
        """Declare who an agent is. Registered agents get conditioned serving.

        Note (engram-lite trust model): profiles are self-declared — anyone with
        access to this store can register any agent name, including one matching
        another writer's origin_tool. Multi-user access control is out of
        scope for engram-lite; the trust boundary is the SQLite file itself.
        """
        norm_domain = tagging.normalize(domain)
        if not norm_domain:
            # an empty domain would silently disable the cross-domain partition
            # for this agent (the gate skips falsy domains) — refuse it.
            raise ValueError(f"domain {domain!r} normalizes to empty; "
                             "the domain partition needs a real domain")
        clean = [t for t in (tagging.normalize(t) for t in scope_tags) if t]
        repository.upsert_profile(
            self.conn, agent=agent, persona=persona,
            domain=norm_domain, scope_tags_json=json.dumps(clean),
        )
        self.retag()   # facts written before this scope existed heal onto it
        # facts this agent captured BEFORE its profile existed carry domain=NULL,
        # and NULL-domain facts pass every agent's domain gate — backfill them.
        repository.backfill_domain(self.conn, agent, norm_domain)
        return {"agent": agent, "persona": persona,
                "domain": norm_domain, "scope_tags": clean}

    @_locked
    def retag(self) -> int:
        """Re-canonicalize every current fact's tags against the PROFILE scopes.

        Tags written before a profile existed can drift ('market-sizing' saved
        before anyone declared 'market-size') — and drifted tags self-legitimize
        because stored tags feed the vocabulary. Canonicalizing against scope
        tags only (not the full vocabulary) heals the store; unknown tags with
        no scope relative are kept as-is. Runs on every profile registration;
        O(current facts), milliseconds at lite scale. Returns rows changed.
        """
        scopes: set = set()
        for p in self.profiles():
            scopes.update(p["scope_tags"])
        if not scopes:
            return 0
        changed = 0
        for row in repository.all_current(self.conn):
            old = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            new = tagging.canonicalize(old, scopes)
            if new != old:
                repository.update_tags(self.conn, row["id"], json.dumps(new))
                changed += 1
        return changed

    @_locked
    def profile(self, agent: Optional[str]) -> Optional[dict]:
        if not agent:
            return None
        row = repository.get_profile(self.conn, agent)
        if row is None:
            return None
        return {"agent": row["agent"], "persona": row["persona"], "domain": row["domain"],
                "scope_tags": json.loads(row["scope_tags"])}

    @_locked
    def profiles(self) -> List[dict]:
        return [{"agent": r["agent"], "persona": r["persona"], "domain": r["domain"],
                 "scope_tags": json.loads(r["scope_tags"])}
                for r in repository.all_profiles(self.conn)]

    @_locked
    def vocabulary(self) -> set:
        """Known tags: every profile's scope + every tag already in the store."""
        vocab: set = set()
        for p in self.profiles():
            vocab.update(p["scope_tags"])
        for tags_json in repository.all_current_tags(self.conn):
            vocab.update(json.loads(tags_json))
        return vocab

    # ── REMEMBER (gated) ──────────────────────────────────────────────────────
    @_locked
    def remember(self, text: str, subject: Optional[str] = None,
                 tags: Optional[List[str]] = None,
                 speaker: Optional[str] = None,
                 when: Optional[str] = None) -> dict:
        """Save *only if worth it* — scrub secrets, extract atomic facts, gate each.

        This is what tools should call. Pipeline:
          1. scrub secrets (never store API keys/tokens);
          2. skip only true whole-message junk (artifacts, greetings, code dumps);
          3. extract the discrete facts, judge EVERY fragment on its own merits.

        A gate rule may kill a sentence, never a turn: the old ordering judged
        the whole message first, so "I got a new job last week. How about you?"
        died on its trailing question mark — 100% of measured write-path loss
        came from that one inversion (loss census 2026-07-05).

        speaker/when: conversational anchors. Extraction splits a turn into
        fragments, and fragments 2..n shed the who/when that only the opening
        carried (66% of stored fragments were unanchored). When provided, every
        stored fragment is re-anchored with them.

        Returns the single save result when the text is one fact; otherwise a
        {"decision": "MULTI", "saved": N, "results": [...]} summary (with
        "truncated": true when the per-interaction fact cap cut candidates).
        A non-worthy input returns {"decision": "SKIP", "reason": ...}.
        "redacted" lists any secrets removed.
        """
        clean, secrets = redaction.scrub(text)        # never store API keys/tokens
        prose = salience.strip_fences(clean)          # code spans never veto the prose
        gate = salience.assess(prose)
        if not gate.keep and gate.reason in _FATAL_WHOLE_MESSAGE:
            repository.record_decision(self.conn, "capture-skip", gate.reason, prose)
            return {"decision": "SKIP", "reason": gate.reason, "redacted": secrets}
        if len(prose) > config.MAX_TEXT_CHARS * 5:
            # extraction handles long human messages; something THIS big is a dump
            reason = "too long — looks like a file or output dump"
            repository.record_decision(self.conn, "capture-skip", reason, prose)
            return {"decision": "SKIP", "reason": reason, "redacted": secrets}

        candidates, truncated = extraction.extract_full(prose)
        anchor_date = dates.parse_anchor(when) if when else None
        hist = anchor_date.isoformat() if anchor_date else None
        if anchor_date:
            # "I went last Friday" carries its date only relative to the
            # conversation timestamp — resolve it with calendar arithmetic at
            # capture (LLM-extraction pipelines do this with a model call; we
            # do it with code).
            # The fact's created_at also becomes the historical date, so
            # recency and version ordering reflect when it was TRUE.
            candidates = [dates.resolve_relatives(c, anchor_date) for c in candidates]
        if truncated:
            repository.record_decision(
                self.conn, "truncation",
                f"per-interaction fact cap ({config.MAX_FACTS_PER_INTERACTION}) hit; "
                "kept most informative", prose)

        results: List[dict] = []
        for candidate in candidates:                  # one interaction → atomic facts
            if salience.is_noise_fragment(candidate):
                repository.record_decision(self.conn, "fragment-skip",
                                           "noise fragment", candidate)
                continue                              # meta/imperative/code fragment
            verdict = salience.assess(candidate)
            if not verdict.keep:
                repository.record_decision(self.conn, "fragment-skip",
                                           verdict.reason, candidate)
                continue                              # drop junk sub-fragments
            results.append(self.save(self._anchored(candidate, speaker, when),
                                     subject=subject or speaker, tags=tags,
                                     fragment_type=verdict.fragment_type,
                                     volatility_class=verdict.volatility_class,
                                     created_at=hist))
        if not results:
            # extraction reduced everything to noise fragments. Keep the message
            # intact ONLY if the whole text stands on its own as a non-noise
            # fact — otherwise this fallback would store exactly the junk the
            # filters rejected ("Ready to execute when you are." → ADD;
            # red-team round 2).
            if salience.is_noise_fragment(prose) or not salience.assess(prose).keep:
                repository.record_decision(self.conn, "capture-skip",
                                           "all fragments were noise", prose)
                return {"decision": "SKIP", "reason": "all fragments were noise",
                        "redacted": secrets}
            results.append(self.save(self._anchored(prose, speaker, when),
                                     subject=subject or speaker, tags=tags,
                                     fragment_type=gate.fragment_type,
                                     volatility_class=gate.volatility_class,
                                     created_at=hist))

        if len(results) == 1:
            res = results[0]
            if secrets:
                res["redacted"] = secrets
            return res

        saved = sum(1 for r in results
                    if r["decision"] in ("ADD", "UPDATE", "DELETE", "REINFORCE"))
        summary = {"decision": "MULTI", "saved": saved, "results": results}
        if truncated:
            summary["truncated"] = True   # never a silent cut (loss census P1)
        if secrets:
            summary["redacted"] = secrets
        return summary

    @staticmethod
    def _anchored(fragment: str, speaker: Optional[str], when: Optional[str]) -> str:
        """Re-attach conversational anchors a split fragment may have shed."""
        out = fragment
        if speaker and speaker.lower() not in out.lower():
            out = f"{speaker}: {out}"
        if when and str(when).lower() not in out.lower():
            out = f"{out} (on {when})"
        return out

    # ── SAVE (unconditional store) ────────────────────────────────────────────
    @_locked
    def save(self, text: str, subject: Optional[str] = None, fragment_type: str = "fact",
             volatility_class: str = "static", valid_until: Optional[str] = None,
             tags: Optional[List[str]] = None, domain: Optional[str] = None,
             created_at: Optional[str] = None) -> dict:
        """created_at (ISO string): historical timestamp for imports/replays —
        recency and version ordering then reflect when the fact was TRUE, not
        when it was ingested. Default: now."""
        tags = _tag_list(tags)                 # 'hiring' must never become ['h','i',...]
        text = text.strip()
        key = compaction.compact_key(text)     # short label — embedded + the handle
        value = compaction.cap_value(text)     # full text, capped as a guardrail
        block_id = subjects.resolve(text, subject)
        vec = self.embedder.embed(key)         # embed the compact key (cleaner match)

        op, target, demote_note = consolidation.decide(self.conn, block_id, vec, key)
        if demote_note:
            # an UPDATE/REINFORCE the anchor guard turned into ADD — auditable
            repository.record_decision(self.conn, "merge-demote", demote_note, key)

        if op == "REINFORCE":
            repository.bump_confidence(self.conn, target, 0.05)
            return {"decision": "REINFORCE", "fact_id": target, "block_id": block_id}

        vocab = self.vocabulary()
        # canonicalize caller/model tags against the vocabulary so synonyms
        # ('database' vs 'db') don't fragment lanes
        fact_tags = tagging.extract(text, vocab,
                                    explicit=tagging.canonicalize(tags, vocab))
        if domain is None:
            writer = self.profile(self.origin_tool)
            domain = writer["domain"] if writer else None

        prior = target if op == "UPDATE" else None
        fid = self._insert(key, value, vec, block_id, fragment_type,
                           volatility_class, valid_until, prior_version_id=prior,
                           tags_json=json.dumps(fact_tags), domain=domain,
                           created_at=created_at)
        # supersede/invalidate BEFORE eviction: eviction must see the retired
        # row as dead weight, or it hard-deletes an innocent live fact while
        # the about-to-die one survives (loss census P1 — one casualty per
        # UPDATE at the store cap)
        if op == "UPDATE":
            repository.mark_superseded(self.conn, target, fid)
        elif op == "DELETE":
            # the new fact contradicts a prior belief: store the new one, retire the old.
            repository.invalidate(self.conn, target)
        repository.sweep_expired(self.conn, repository.today_iso())  # mark expired facts dead
        eviction.enforce(self.conn, self.max_facts)   # keep the store under its size cap
        if op in ("UPDATE", "DELETE"):
            return {"decision": op, "fact_id": fid, "block_id": block_id,
                    "superseded": target, "tags": fact_tags}
        return {"decision": "ADD", "fact_id": fid, "block_id": block_id, "tags": fact_tags}

    # ── FIND ────────────────────────────────────────────────────────────────
    @_locked
    def search(self, query: str, subject: Optional[str] = None,
               k: int = config.DEFAULT_TOP_K, validate: bool = True,
               agent: Optional[str] = None,
               task_tags: Optional[List[str]] = None) -> List[dict]:
        """Recall memory. Multi-signal search by default; conditioned when possible.

        If `agent` names a registered profile, results are served through the
        lane model (promotion): scoped to the agent's persona/domain/task, with
        a relevance floor and honest abstention (an empty list when the
        situation is outside the agent's lane). Without a profile, behavior is
        plain three-signal retrieval.
        """
        task_tags = _tag_list(task_tags)   # 'hiring' must never char-split → false abstain
        k = max(1, min(int(k), config.SEARCH_K_CAP))
        block_id = subjects.resolve(query, subject) if subject else None
        prof = self.profile(agent)

        if prof is None:
            hits = retrieval.search(self.conn, self.embedder, query,
                                    block_id=block_id, k=k, validate=validate)
            return [self._parse(h) for h in hits]

        # conditioned path: over-fetch, then promote for THIS agent
        fetch = max(k * config.PROMOTION_OVERFETCH, config.PROMOTION_MIN_CANDIDATES)
        candidates = retrieval.search(self.conn, self.embedder, query,
                                      block_id=block_id, k=fetch, validate=validate,
                                      touch=False)
        candidates = [self._parse(c) for c in candidates]

        if task_tags is None:
            task_tags = tagging.extract(query, self.vocabulary())
        else:
            # generic framework stamps ('conversation', 'chat') are not task
            # conditioning — treating them as a lane caused a TOTAL silent
            # serving blackout for integrations that stamp every call with
            # them (loss census P1). Strip them; what remains decides the path.
            task_tags = [t for t in task_tags
                         if tagging.normalize(t) not in config.GENERIC_TASK_TAGS]
            # canonicalize model-chosen task tags too ('migration' → 'migrations')
            supplied = task_tags
            task_tags = tagging.canonicalize(task_tags, self.vocabulary())
            if supplied and not task_tags:
                # the caller DID condition, with tags that are pure garbage
                # ('@@@'). Falling back here would serve MORE than a real
                # out-of-scope tag does (which abstains) — abstain instead.
                repository.record_decision(self.conn, "serve-abstain",
                                           "task tags canonicalized to nothing",
                                           query)
                return []
        if not task_tags:
            # nothing to build a lane from: fall back to flat, honestly flagged —
            # but the hard domain partition STILL holds. Without this, a tagless
            # query is a hole in conditioning: any agent could be served any
            # discipline's memory just by phrasing a question in words outside
            # the vocabulary (observed live: a recruiter asking "where is the
            # postmortem template?" was served the SRE's facts).
            agent_domain = prof.get("domain")
            pool = [c for c in candidates
                    if not (agent_domain and c.get("domain")
                            and c["domain"] != agent_domain)]
            repository.record_decision(self.conn, "serve-fallback",
                                       "no task tags derivable from query", query)
            for c in pool[:k]:
                c["promotion"] = {"mode": "fallback", "reason": "no task tags derivable"}
                repository.touch_access(self.conn, c["id"])
            return pool[:k]

        # the LANE CHANNEL (4th signal): every current fact tagged in this
        # agent's lane joins the candidate pool directly, so text retrieval
        # (keyword miss, vector floor) can never starve promotion of a fact
        # the lane genuinely owns. Promotion still gates domain + scores.
        lane = set(task_tags) & set(prof["scope_tags"])
        if lane:
            today = repository.today_iso()
            seen_ids = {c["id"] for c in candidates}
            pool = list(repository.facts_by_tags(self.conn, sorted(lane),
                                                 config.LANE_FETCH_LIMIT))
            # the PROVENANCE channel (5th signal): the agent's own captures
            # join the pool too, so a fact whose text carried no in-scope
            # vocabulary word (untaggable, invisible to both the lane channel
            # and often to text retrieval) is still promotable to its author.
            pool += list(repository.facts_by_origin(self.conn, prof["agent"],
                                                    config.LANE_FETCH_LIMIT))
            for r in pool:
                row = self._parse(dict(r))
                if row["id"] in seen_ids:
                    continue
                seen_ids.add(row["id"])
                if validate and row["valid_until"] and row["valid_until"] < today:
                    continue
                candidates.append(row)

        idf = promotion.idf_weights(
            [json.loads(t) for t in repository.all_current_tags(self.conn)])
        served = promotion.promote(candidates, prof, task_tags, query, k, idf=idf)
        served = retrieval.collapse_near_dups(served)   # paraphrase dupes waste slots
        for s in served:
            # a serve is a serve — origin-v1 included. Not counting them made
            # an agent's own vocabulary-untagged facts starve at eviction while
            # once-served junk survived (loss census P1); junk is now stopped
            # at capture, so access counting can be honest again.
            repository.touch_access(self.conn, s["id"])
        return served

    @_locked
    def lane_snapshot(self, agent: str, k: int = 8) -> List[dict]:
        """The top of THIS agent's lane — a tag query, not a text query.

        Session-start injection ("what do I already know?") has no meaningful
        query text, so routing it through text retrieval starves it as the
        store grows (nothing keyword-matches "session start", and the vector
        floor drops the rest). This promotes directly over ALL current facts:
        domain gate + lane scoring + relevance floor + dedup, no text funnel.
        """
        prof = self.profile(agent)
        if prof is None:
            return []
        today = repository.today_iso()
        candidates = [row for row in
                      (self._parse(r) for r in repository.all_current(self.conn))
                      # same expiry guard as search(): 'fresh' status alone does
                      # not mean unexpired until the sweep has run
                      if not (row["valid_until"] and row["valid_until"] < today)]
        idf = promotion.idf_weights(
            [json.loads(t) for t in repository.all_current_tags(self.conn)])
        served = promotion.promote(candidates, prof, prof["scope_tags"],
                                   query="", k=k, idf=idf)
        served = retrieval.collapse_near_dups(served)
        for s in served:
            repository.touch_access(self.conn, s["id"])   # a serve is a serve
        return served

    # ── FORGET / UPDATE ───────────────────────────────────────────────────────
    @_locked
    def forget(self, fact_id: str, reason: str = "manual") -> None:
        """Mark a fact no longer valid (kept in history, just not served)."""
        repository.invalidate(self.conn, fact_id)

    @_locked
    def supersede(self, fact_id: str, new_text: str) -> dict:
        row = repository.get(self.conn, fact_id)
        if row is None:
            raise KeyError(fact_id)
        return self.save(new_text, subject=row["subject_key"])

    # ── helpers ───────────────────────────────────────────────────────────────
    @_locked
    def all_current(self) -> List[dict]:
        return [self._parse(r) for r in repository.all_current(self.conn)]

    @classmethod
    def reembed(cls, path: str, embedder: Optional[Embedder] = None) -> dict:
        """Rebuild the vector table with a (new) embedding model.

        A store is bound to its embedder's dimension; switching models
        (bge-small 384-d → bge-base 768-d) requires re-embedding every fact's
        key — deterministic, local, ~seconds for typical stores. Also the
        foundation for importing stores built elsewhere.
        """
        import sqlite3 as _sq

        import sqlite_vec as _sv

        from ..storage import schema
        emb = embedder or get_embedder()
        conn = _sq.connect(path)
        try:
            conn.enable_load_extension(True)
            _sv.load(conn)
            conn.enable_load_extension(False)
            conn.execute("DROP TABLE IF EXISTS facts_vec")
            conn.execute(schema.vec_table_sql(emb.dim))
            rows = conn.execute("SELECT id, key FROM facts").fetchall()
            for fid, key in rows:
                conn.execute(
                    "INSERT INTO facts_vec (fact_id, embedding) VALUES (?, ?)",
                    (fid, _sv.serialize_float32(emb.embed(key))))
            conn.commit()
        finally:
            conn.close()
        return {"reembedded": len(rows), "dim": emb.dim,
                "model": getattr(emb, "model_name", type(emb).__name__)}

    @_locked
    def decisions(self, kind: Optional[str] = None, limit: int = 50) -> List[dict]:
        """The decision ledger: why memory was NOT kept, merged, or served.

        Every drop, demotion, truncation, and serving fallback is recorded with
        the rule that fired — "why don't you remember X?" has an answer instead
        of a shrug. Kinds: capture-skip, fragment-skip, truncation,
        merge-demote, serve-fallback, serve-abstain.
        """
        return [dict(r) for r in
                repository.recent_decisions(self.conn, kind=kind, limit=limit)]

    @_locked
    def stats(self) -> dict:
        """How full is the store? (current vs total facts, the cap, and DB size)."""
        return {
            "facts_current": repository.count_current(self.conn),
            "facts_total": repository.count_facts(self.conn),
            "max_facts": self.max_facts,
            "db_bytes": repository.db_size_bytes(self.conn),
        }

    @_locked
    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _parse(row: dict) -> dict:
        """Row dict → caller dict (tags JSON string → list)."""
        out = dict(row)
        raw = out.get("tags")
        out["tags"] = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return out

    def _insert(self, key, value, vec, block_id, fragment_type,
                volatility_class, valid_until, prior_version_id=None,
                tags_json: str = "[]", domain: Optional[str] = None,
                created_at: Optional[str] = None) -> str:
        fid = str(uuid.uuid4())
        ents = entities.extract(f"{key} {value}")   # index the fact's entities
        repository.insert_fact(
            self.conn, fid=fid, block_id=block_id, subject_key=block_id,
            key=key, value=value, fragment_type=fragment_type, confidence=1.0,
            created_at=created_at or repository.now_iso(), valid_until=valid_until,
            volatility_class=volatility_class, validation_status="fresh",
            origin_tool=self.origin_tool, prior_version_id=prior_version_id, vec=vec,
            entities=ents, tags_json=tags_json, domain=domain,
        )
        return fid
