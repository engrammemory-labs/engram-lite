"""Runtime settings, read from environment variables.

Host tools launch engram with an `env` block, so the *user*
controls behavior locally. Nothing happens unless ENGRAM_AUTOCHECK is true.

  ENGRAM_AUTOCHECK     master switch (default false)
  ENGRAM_READ          allow recall/search             (default true)
  ENGRAM_WRITE         allow save/update               (default true)
  ENGRAM_VALIDATE      apply freshness/expiry on reads (default true)
  ENGRAM_ALLOW_FORGET  allow invalidate/forget         (default false — destructive)
  ENGRAM_DB_PATH       the shared SQLite file          (default ~/.engram/memory.db)
  ENGRAM_AGENT         which tool/agent is calling     (default 'unknown')
  ENGRAM_TOP_K         how many memories to return     (default 5)

Conditioned promotion (optional — set all three and the agent is auto-registered
with a profile on server start, so its recalls are served through the lane model):

  ENGRAM_PERSONA       who this agent is, e.g. 'on-call SRE'
  ENGRAM_DOMAIN        its discipline, e.g. 'sre-devops'
  ENGRAM_SCOPE_TAGS    comma-separated concepts it owns, e.g. 'alert,incident,latency'

See docs/code-docs/INTEGRATIONS.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_TRUTHY = {"1", "true", "yes", "on", "y"}


def _envbool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in _TRUTHY


def _envstr(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _envint(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _envlist(name: str) -> List[str]:
    v = os.environ.get(name, "")
    return [t.strip() for t in v.split(",") if t.strip()]


@dataclass(frozen=True)
class Settings:
    autocheck: bool
    read: bool
    write: bool
    validate: bool
    allow_forget: bool
    db_path: str
    agent: str
    top_k: int
    persona: Optional[str]
    domain: Optional[str]
    scope_tags: List[str]

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = Path.home() / ".engram" / "memory.db"
        return cls(
            autocheck=_envbool("ENGRAM_AUTOCHECK", False),
            read=_envbool("ENGRAM_READ", True),
            write=_envbool("ENGRAM_WRITE", True),
            validate=_envbool("ENGRAM_VALIDATE", True),
            allow_forget=_envbool("ENGRAM_ALLOW_FORGET", False),
            db_path=os.path.expanduser(_envstr("ENGRAM_DB_PATH", str(default_db))),
            agent=_envstr("ENGRAM_AGENT", "unknown"),
            top_k=_envint("ENGRAM_TOP_K", 5),
            persona=os.environ.get("ENGRAM_PERSONA") or None,
            domain=os.environ.get("ENGRAM_DOMAIN") or None,
            scope_tags=_envlist("ENGRAM_SCOPE_TAGS"),
        )

    @property
    def has_profile(self) -> bool:
        return bool(self.persona and self.domain and self.scope_tags)

    def summary(self) -> str:
        base = (
            f"autocheck={self.autocheck} read={self.read} write={self.write} "
            f"validate={self.validate} allow_forget={self.allow_forget} "
            f"agent={self.agent} db={self.db_path}"
        )
        if self.has_profile:
            base += (f" | profile: persona='{self.persona}' domain='{self.domain}' "
                     f"scope={','.join(self.scope_tags)}")
        return base
