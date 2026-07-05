"""EngramMemoryProvider — plug engram-lite into a Hermes agent.

Implements Hermes's MemoryProvider surface (initialize / system_prompt_block /
prefetch / sync_turn / get_tool_schemas / handle_tool_call / shutdown) with NO
hard dependency on the hermes package — Hermes simply instantiates this class
and calls the lifecycle methods, so the same class also works standalone or
with any framework that follows the shape.

What it gives a Hermes agent:
  - persistent memory across restarts (one SQLite file; same path = same memory)
  - auto-capture during the run (`sync_turn` passes each turn through the
    salience gate — junk is skipped, facts are consolidated/de-duped)
  - conditioned serving (register the agent's persona/domain/scope and every
    recall is served for THIS agent's lane, with honest abstention)
  - in-loop tools (memory_search / memory_write) the model can call itself

Minimal wiring:

    from engram.integrations.hermes import EngramMemoryProvider

    memory = EngramMemoryProvider(
        db_path="~/.engram/memory.db",
        agent="oncall_sre",
        persona="on-call SRE",
        domain="sre-devops",
        scope_tags=["alert", "incident", "latency", "trace", "mitigation"],
    )
    # hand `memory` to Hermes as the agent's memory provider

On the next process start with the same db_path, `initialize` reopens the same
store and `system_prompt_block` serves the agent what it already knows.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..core.memory import Memory


class EngramMemoryProvider:
    """Hermes MemoryProvider backed by a local engram-lite store."""

    def __init__(self, db_path: str = "~/.engram/memory.db", agent: str = "hermes-agent",
                 persona: Optional[str] = None, domain: Optional[str] = None,
                 scope_tags: Optional[List[str]] = None, top_k: int = 5,
                 auto_capture: bool = True, embedder: Any = None) -> None:
        """auto_capture (default True): capture every turn through the salience
        gate, EXCEPT a turn where the model already wrote to memory itself (via
        the memory_write tool) — so nothing is lost when the model is passive
        (the common case), and paraphrase duplicates are avoided when it isn't.
        Set False to disable turn capture entirely (the model's explicit
        memory_write calls are the only writes)."""
        self.db_path = os.path.expanduser(db_path)
        self.agent = agent
        self.persona = persona
        self.domain = domain
        self.scope_tags = scope_tags or []
        self.top_k = top_k
        self.auto_capture = auto_capture
        self._wrote_this_turn = False   # did the MODEL memory_write this turn?
        self._embedder = embedder
        self._mem: Optional[Memory] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def initialize(self, context: Any = None) -> "EngramMemoryProvider":
        """Open (or reopen) the store. Called by Hermes at agent start."""
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._mem = Memory(self.db_path, origin_tool=self.agent, embedder=self._embedder)
        if self.persona and self.domain and self.scope_tags:
            self._mem.register_profile(self.agent, self.persona, self.domain,
                                       self.scope_tags)
        return self

    def shutdown(self) -> None:
        if self._mem is not None:
            self._mem.close()
            self._mem = None

    # ── serving ───────────────────────────────────────────────────────────────
    def system_prompt_block(self, max_items: int = 8) -> str:
        """A session-start snapshot: what this agent already knows, in its lane.

        Injected into the system prompt so a restarted agent resumes with its
        memory instead of starting cold.
        """
        mem = self._require()
        if mem.profile(self.agent):
            # a lane query, not a text query: promote straight over the store,
            # bypassing text retrieval (which starves on "session start")
            hits = mem.lane_snapshot(self.agent, k=max_items)
        else:
            hits = mem.all_current()[-max_items:]
        if not hits:
            return ""
        lines = "\n".join(f"- {h['value']}" for h in hits)
        return (
            "## Memory (engram)\n"
            "You have persistent memory. What you already know that fits your role:\n"
            f"{lines}\n"
            "Use the memory_search tool for anything more specific; use memory_write "
            "to store new durable facts and decisions."
        )

    def prefetch(self, query: str, task_tags: Optional[List[str]] = None) -> str:
        """Recall memory relevant to the upcoming task (conditioned when profiled)."""
        mem = self._require()
        hits = mem.search(query, agent=self.agent, task_tags=task_tags, k=self.top_k)
        if not hits:
            return ""
        return "\n".join(f"- {h['value']}" for h in hits)

    # ── capture ───────────────────────────────────────────────────────────────
    def sync_turn(self, user_text: Optional[str] = None,
                  assistant_text: Optional[str] = None, **_: Any) -> Dict[str, int]:
        """Pass a finished turn through the salience gate (auto-capture).

        Junk (code, questions, greetings, dumps) is skipped by the engine, so
        calling this on every turn is safe. Skips a turn where the model already
        wrote to memory itself (avoids paraphrase duplicates); captures every
        other turn so memory accumulates even when the model is passive.
        """
        if not self.auto_capture:
            return {"saved": 0, "skipped": 0, "capture": "auto_capture disabled"}
        if self._wrote_this_turn:                 # the model already saved this turn
            self._wrote_this_turn = False         # reset at the turn boundary
            return {"saved": 0, "skipped": 0, "capture": "model wrote this turn"}
        mem = self._require()
        saved = skipped = 0
        for text in (user_text, assistant_text):
            if not text or not text.strip():
                continue
            res = mem.remember(text)
            if res["decision"] == "SKIP":
                skipped += 1
            elif res["decision"] == "MULTI":     # one turn can yield several facts
                saved += res["saved"]
            else:
                saved += 1
        return {"saved": saved, "skipped": skipped}

    # ── in-loop tools ─────────────────────────────────────────────────────────
    def get_tool_schemas(self, style: str = "anthropic") -> List[Dict[str, Any]]:
        """Tool definitions the model can call mid-run — any LLM backend.

        style="anthropic" (default) returns Anthropic-shaped tools;
        style="openai" returns OpenAI function-calling shape (also what most
        OpenAI-compatible local servers expect). The engine itself is
        model-neutral; this only adapts the schema envelope.
        """
        if style == "openai":
            return [{"type": "function",
                     "function": {"name": t["name"], "description": t["description"],
                                  "parameters": t["input_schema"]}}
                    for t in self.get_tool_schemas("anthropic")]
        return [
            {
                "name": "memory_search",
                "description": ("Recall relevant long-term memories before answering. "
                                "Optional task_tags sharpen what the situation is about. "
                                "An empty result for a profiled agent means the topic is "
                                "outside this agent's lane."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "task_tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_write",
                "description": ("Store a durable fact, decision, or preference. The engine "
                                "skips junk and de-duplicates, so call freely."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "memory_diagnose",
                "description": ("Explain why memory behaved the way it did: the most "
                                "recent capture skips, merges, truncations, and serving "
                                "fallbacks, each with the exact rule that fired. Use when "
                                "the user asks why something isn't remembered or a recall "
                                "looks wrong — the answer is in this ledger, not a guess."),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": ["capture-skip", "fragment-skip", "truncation",
                                          "merge-demote", "serve-fallback", "serve-abstain"],
                                 "description": "filter to one decision kind (optional)"},
                        "limit": {"type": "integer", "description": "max entries (default 20)"},
                    },
                },
            },
        ]

    def handle_tool_call(self, name: str, arguments: Any) -> str:
        """Execute one of the tools from get_tool_schemas. Returns a string result."""
        mem = self._require()
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        args = arguments or {}
        if name == "memory_search":
            hits = mem.search(args["query"], agent=self.agent,
                              task_tags=args.get("task_tags"), k=self.top_k)
            if not hits:
                if mem.profile(self.agent):
                    return "(nothing in this agent's lane — correctly abstaining)"
                return "(no relevant memories)"
            return "\n".join(f"- {h['value']}" for h in hits)
        if name == "memory_write":
            res = mem.remember(args["text"], tags=args.get("tags"))
            if res["decision"] != "SKIP":
                self._wrote_this_turn = True    # skip this turn's auto-capture (no dupe)
            note = f" (redacted: {', '.join(res['redacted'])})" if res.get("redacted") else ""
            if res["decision"] == "SKIP":
                return f"skipped — {res['reason']}{note}"
            if res["decision"] == "MULTI":     # extraction split it into several facts
                return f"MULTI: saved {res['saved']} facts{note}"
            return f"{res['decision']} (id {res['fact_id'][:8]}){note}"
        if name == "memory_diagnose":
            rows = mem.decisions(kind=args.get("kind"),
                                 limit=min(int(args.get("limit") or 20), 100))
            if not rows:
                return "(no recorded memory decisions yet)"
            return "\n".join(
                f"- [{r['ts'][:19]}] {r['kind']}: {r['rule']}"
                + (f" — “{r['snippet']}”" if r.get("snippet") else "")
                for r in rows)
        raise ValueError(f"unknown tool: {name}")

    # ── internal ──────────────────────────────────────────────────────────────
    def _require(self) -> Memory:
        if self._mem is None:
            self.initialize()
        assert self._mem is not None
        return self._mem
