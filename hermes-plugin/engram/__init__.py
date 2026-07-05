"""engram — conditioned local memory for Hermes agents.

Serves each Hermes profile the memory that fits its persona, domain, and the
task at hand (the lane model): honest abstention when a topic is outside the
agent's lane, hard domain isolation between profiles sharing one store, and
restart-proof recall from one local SQLite file. No API keys, no network —
the engine never calls an LLM.

Install (user plugin):
    mkdir -p ~/.hermes/plugins && cp -R hermes-plugin/engram ~/.hermes/plugins/
    pip install engram-lite            # into the python env that runs hermes
Activate:
    config.yaml →  memory.provider: engram
Configure (optional — defaults work):
    hermes memory setup                # walks persona / domain / scope_tags
    # or write <HERMES_HOME>/engram/config.json directly

This module is a thin envelope adapter: all memory behavior lives in the
engram-lite package (engram.integrations.hermes.EngramMemoryProvider); this
class translates Hermes's MemoryProvider ABC onto it.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_CONFIG_FILE = "config.json"

# persona → (domain, scope_tags). The user answers ONE question ("what is this
# agent?"); the taxonomy is our job. First keyword found in the persona wins;
# packs are the ones validated on a real multi-agent store (0 cross-domain
# leaks, 100% in-lane recall). Unknown personas fall back to a kebab-cased
# domain of their own — still fully domain-isolated, and the provenance
# channel serves an agent its own captures even before tags accumulate.
_ROLE_PACKS: List[tuple] = [
    (("sre", "on-call", "oncall", "site reliability", "incident"),
     "sre-devops", ["alert", "incident", "latency", "trace", "mitigation",
                    "db", "oncall", "error", "pager", "runbook"]),
    (("devops", "platform engineer", "infra", "release engineer"),
     "sre-devops", ["deploy", "release", "rollback", "canary", "capacity",
                    "nodes", "k8s", "upgrade", "ingress", "freeze"]),
    (("security", "appsec", "infosec"),
     "security-engineering", ["security", "vulnerability", "cve", "patch",
                              "secrets", "rotation", "auth", "audit",
                              "compliance", "policy"]),
    (("backend", "api engineer"),
     "backend-engineering", ["api", "endpoints", "db", "queries", "schema",
                             "migrations", "index", "cache", "idempotency",
                             "latency"]),
    (("frontend", "ui engineer", "web engineer"),
     "frontend-engineering", ["ui", "components", "bundle", "css", "react",
                              "accessibility", "forms", "checkout",
                              "hydration", "state"]),
    (("data scientist", "ml engineer", "machine learning", "data analyst"),
     "data-science", ["model", "dataset", "training", "evaluation",
                      "experiment", "pipeline", "accuracy", "drift",
                      "fraud", "features"]),
    (("product manager", "product owner", " pm"),
     "product-mgmt", ["roadmap", "feature", "launch", "metrics", "conversion",
                      "experiment", "feedback", "pricing", "checkout",
                      "users"]),
    (("recruit", "talent", "sourcing"),
     "talent-acquisition", ["hiring", "pipeline", "interview", "offer",
                            "candidate", "headcount", "sourcing", "referral",
                            "joining"]),
    (("hr", "people ops", "people partner", "human resources"),
     "people-ops", ["benefits", "policy", "leave", "insurance", "payroll",
                    "onboarding", "grievance", "wellness", "training"]),
    (("support", "customer success", "helpdesk"),
     "customer-support", ["ticket", "customer", "escalation", "refund", "sla",
                          "complaint", "resolution", "workaround", "otp"]),
    (("chief of staff", "executive assistant", "exec"),
     "exec-ops", ["priorities", "okrs", "decisions", "board", "stakeholders",
                  "meetings", "investor-updates", "follow-ups"]),
    (("coach", "mentor"),
     "coaching", ["goals", "habits", "feedback", "development", "wellbeing",
                  "burnout", "one-on-one", "delegation"]),
    (("content", "writer", "copywriter", "marketing"),
     "content", ["blog", "copy", "tone", "style", "headlines", "publishing",
                 "calendar", "launch", "seo"]),
    (("market research", "market analyst"),
     "market-research", ["market-size", "trends", "segments", "surveys",
                         "pricing", "tam", "analysts", "demand"]),
    (("startup research", "competitive", "competitor"),
     "startup-research", ["competitors", "funding", "founders",
                          "product-launches", "positioning", "whitespace",
                          "moat"]),
    (("research",),   # generic researcher — after the specific research roles
     "research", ["sources", "findings", "papers", "citations", "hypotheses",
                  "experiments", "notes", "summaries"]),
]


def derive_profile(persona: str) -> tuple:
    """(domain, scope_tags) for a persona — deterministic, no questions asked."""
    low = f" {persona.strip().lower()} "
    for keys, domain, scope in _ROLE_PACKS:
        if any(k in low for k in keys):
            return domain, list(scope)
    # unknown role: its own kebab-case domain (fully isolated) + the persona's
    # own content words as a seed scope; tags accumulate as it works
    words = [w for w in "".join(c if c.isalnum() or c.isspace() else " "
                                for c in low).split() if len(w) > 2]
    domain = "-".join(words[:3]) or "general"
    return domain, words or ["notes"]


class EngramHermesProvider(MemoryProvider):
    """Hermes MemoryProvider backed by a local engram-lite store."""

    def __init__(self) -> None:
        self._inner = None            # engram.integrations.hermes.EngramMemoryProvider
        self._primary = True          # only the primary agent context writes
        self._home: Optional[str] = None
        self._pending: List[tuple] = []   # turns buffered for the guaranteed flush
        self._config_error: Optional[str] = None  # half-configured conditioning → loud + safe

    # -- identity / availability ---------------------------------------------
    @property
    def name(self) -> str:
        return "engram"

    def is_available(self) -> bool:
        """Local-only provider: available iff the engram-lite package imports."""
        try:
            import engram  # noqa: F401
            return True
        except Exception:
            return False

    # -- lifecycle -------------------------------------------------------------
    @staticmethod
    def _config_path(hermes_home: str) -> Path:
        return Path(hermes_home) / "engram" / _CONFIG_FILE

    def initialize(self, session_id: str, **kwargs) -> None:
        from engram.integrations.hermes import EngramMemoryProvider as Inner

        home = str(kwargs.get("hermes_home") or os.path.expanduser("~/.hermes"))
        self._home = home
        # cron/subagent/flush contexts must not corrupt the store with writes
        self._primary = kwargs.get("agent_context", "primary") == "primary"

        cfg: Dict[str, Any] = {}
        cfg_path = self._config_path(home)
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text())
            except Exception as e:
                logger.warning("engram: unreadable %s (%s) — using defaults", cfg_path, e)

        agent = cfg.get("agent") or kwargs.get("agent_identity") or "hermes"
        scope = cfg.get("scope_tags") or []
        if isinstance(scope, str):
            scope = [t.strip() for t in scope.split(",") if t.strip()]
        db_path = cfg.get("db_path") or str(Path(home) / "engram" / "memory.db")

        # top_k: a config typo must never blackout serving (negative), break
        # every search (huge), or crash startup (non-numeric) — clamp loudly.
        raw_k = cfg.get("top_k", 5)
        try:
            top_k = int(raw_k or 5)
        except (TypeError, ValueError):
            logger.warning("engram: top_k %r is not a number — using 5", raw_k)
            top_k = 5
        if not 1 <= top_k <= 50:
            logger.warning("engram: top_k %s out of range — clamped", top_k)
            top_k = min(max(top_k, 1), 50)

        # The user tells us WHO the agent is (one wizard question); we derive
        # the rest. domain/scope_tags in config.json still win when present
        # (power users edit the file), but nobody is asked for taxonomy.
        persona, domain = cfg.get("persona"), cfg.get("domain")
        self._config_error = None
        if persona and not (domain and scope):
            d_domain, d_scope = derive_profile(persona)
            domain = domain or d_domain
            scope = scope or d_scope
            logger.info("engram: derived domain=%s scope=%s from persona %r",
                        domain, ",".join(scope), persona)
        elif not persona and (domain or scope):
            # taxonomy without an identity — refuse to guess: serve SAFE,
            # capture continues (red-team round 2: silent degradation leaked
            # across domains).
            self._config_error = (
                "engram config has domain/scope_tags but no persona — "
                "conditioned serving is PAUSED so memory cannot leak across "
                "domains. Capture still works. Set persona in "
                "~/.hermes/engram/config.json or run: hermes memory setup")
            logger.warning(self._config_error)

        self._inner = Inner(
            db_path=db_path,
            agent=agent,
            persona=persona,
            domain=domain,
            scope_tags=scope,
            top_k=top_k,
        ).initialize()
        logger.info("engram memory ready (agent=%s, db=%s, primary=%s)",
                    agent, db_path, self._primary)

    def shutdown(self) -> None:
        self._flush("shutdown")   # guaranteed inline flush before the process exits
        if self._inner is not None:
            try:
                self._inner.shutdown()
            finally:
                self._inner = None

    def _flush(self, why: str) -> None:
        """Write any buffered turns synchronously. Re-seen facts consolidate to
        REINFORCE, so this is safe even if sync_turn already wrote some."""
        if self._inner is None or not self._primary:
            self._pending = []
            return
        pending, self._pending = self._pending, []
        saved = 0
        for user_text, asst_text in pending:
            for text in (user_text, asst_text):
                if not text or not text.strip():
                    continue
                res = self._inner_handle_remember(text)
                if res and res.get("decision") in ("ADD", "UPDATE", "DELETE", "MULTI"):
                    saved += 1

    # -- serving ----------------------------------------------------------------
    def system_prompt_block(self) -> str:
        if self._inner is None:
            return ""
        if self._config_error:
            return f"## Memory\n⚠ {self._config_error}"
        try:
            return self._inner.system_prompt_block()
        except Exception as e:
            logger.warning("engram system_prompt_block failed: %s", e)
            return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._inner is None or not query or self._config_error:
            return ""   # a half-configured profile must never serve cross-domain
        try:
            return self._inner.prefetch(query)   # local, milliseconds — no thread needed
        except Exception as e:
            logger.warning("engram prefetch failed: %s", e)
            return ""

    # -- capture ------------------------------------------------------------------
    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        if self._inner is None or not self._primary:
            return
        # Buffer first (instant, survives a racing exit), then attempt the write.
        # shutdown()/on_session_end flush the buffer inline as the safety net.
        # Capture the USER's turn only. Facts come from what the user tells the
        # agent; the assistant mostly acknowledges/discusses (that was the noise
        # in early runs). When the assistant establishes a real fact, the model
        # writes it deliberately via memory_write.
        self._pending.append((user_content, None))
        try:
            self._inner.sync_turn(user_text=user_content, assistant_text=None)
            self._pending.pop()   # write landed — no need to re-flush this turn
        except Exception as e:
            logger.warning("engram sync_turn failed: %s", e)

    # -- in-loop tools ---------------------------------------------------------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Hermes expects the FLAT OpenAI shape: name / description / parameters."""
        if self._inner is None:
            return []
        return [{"name": t["name"], "description": t["description"],
                 "parameters": t["input_schema"]}
                for t in self._inner.get_tool_schemas()]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._inner is None:
            return json.dumps({"error": "engram is not initialized"})
        if tool_name == "memory_search" and self._config_error:
            return json.dumps({"error": self._config_error})
        if tool_name == "memory_write" and not self._primary:
            return json.dumps({"skipped": "memory writes are disabled outside the "
                                          "primary agent context"})
        try:
            return json.dumps({"result": self._inner.handle_tool_call(tool_name, args or {})})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # -- optional hooks -----------------------------------------------------------
    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror Hermes's built-in memory writes into the engram store."""
        if self._inner is None or not self._primary:
            return
        if action == "remove" or not content:
            return
        try:
            res = self._inner_handle_remember(content)
            if res:
                logger.debug("engram mirrored builtin memory write: %s", res.get("decision"))
        except Exception as e:
            logger.debug("engram on_memory_write mirror failed: %s", e)

    def _capture_messages(self, messages: List[Dict[str, Any]], why: str) -> int:
        """Run each user/assistant message through the gated store (synchronously).

        Re-seeing an already-stored fact consolidates to REINFORCE (no new row),
        so this is safe to run even when per-turn sync already captured some of
        it — the engine dedups. This is the RELIABLE capture path: unlike
        sync_turn (dispatched to a daemon thread that a oneshot/cron process can
        abandon on exit), on_session_end / on_pre_compress run inline.
        """
        if self._inner is None or not self._primary:
            return 0
        saved = 0
        for m in messages or []:
            if m.get("role") != "user":   # facts come from the user; assistant discusses
                continue
            content = m.get("content")
            if isinstance(content, list):   # anthropic-style content blocks
                content = " ".join(b.get("text", "") for b in content
                                   if isinstance(b, dict) and b.get("type") == "text")
            if not isinstance(content, str) or not content.strip():
                continue
            res = self._inner_handle_remember(content)
            if res and res.get("decision") in ("ADD", "UPDATE", "DELETE", "MULTI"):
                saved += 1
        if saved:
            logger.info("engram: captured %d fact group(s) at %s", saved, why)
        return saved

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Session boundary (CLI/oneshot exit, /reset, gateway expiry): flush.

        The guaranteed capture path — inline, so a single-turn oneshot whose
        background sync got abandoned on exit still persists its memory here.
        """
        self._capture_messages(messages, "session end")

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Capture durable facts from messages about to be compressed away —
        whatever the compressor drops is gone from context; save it first."""
        self._capture_messages(messages, "pre-compression")
        return ""

    def backup_paths(self) -> List[str]:
        """Store lives under HERMES_HOME, which `hermes backup` already walks."""
        return []

    # -- setup flow (`hermes memory setup`) --------------------------------------
    def get_config_schema(self) -> List[Dict[str, Any]]:
        # ONE question. The taxonomy (domain, scope tags, ids, paths) is our
        # job — derived from the persona, written to config.json where power
        # users can still edit every knob.
        return [
            {"key": "persona",
             "description": ("What is this agent? A few words, e.g. 'chief of "
                             "staff', 'product manager', 'DevOps engineer'. "
                             "Leave empty for plain memory."),
             "required": False},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = self._config_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        current: Dict[str, Any] = {}
        if path.exists():
            try:
                current = json.loads(path.read_text())
            except Exception:
                pass
        new_persona = (values or {}).get("persona")
        persona_changed = bool(new_persona) and new_persona != current.get("persona")
        current.update({k: v for k, v in (values or {}).items() if v not in (None, "")})
        # derive the taxonomy from the persona and PERSIST it — visible and
        # editable in config.json, but never asked for in the wizard. When the
        # wizard sets a NEW persona, the derived fields are refreshed too:
        # stale taxonomy from a previous identity must never survive a rename.
        persona = current.get("persona")
        if persona and (persona_changed
                        or not (current.get("domain") and current.get("scope_tags"))):
            domain, scope = derive_profile(persona)
            current["domain"] = domain
            current["scope_tags"] = ",".join(scope)
        path.write_text(json.dumps(current, indent=2) + "\n")

    # -- internal ---------------------------------------------------------------
    def _inner_handle_remember(self, text: str) -> Optional[Dict[str, Any]]:
        """Route free text through the inner engine's gated remember()."""
        try:
            mem = self._inner._require()   # the engram Memory engine
            return mem.remember(text)
        except Exception as e:
            logger.debug("engram remember failed: %s", e)
            return None