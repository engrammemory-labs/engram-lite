"""Persona → (domain, scope_tags): the taxonomy is our job, not the user's.

Integrations ask ONE question — "what is this agent?" — and derive the rest
from this table. First keyword found in the persona wins; the packs are the
ones validated on a real multi-agent store (0 cross-domain leaks, 100%
in-lane recall). Unknown personas get a kebab-cased domain of their own —
still fully domain-isolated, and the provenance channel serves an agent its
own captures even before tags accumulate.

Shared by the Hermes plugin, `engram serve` (the OpenClaw sidecar), and any
future adapter, so every integration resolves personas identically.
"""
from __future__ import annotations

from typing import List, Tuple

ROLE_PACKS: List[tuple] = [
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


def derive_profile(persona: str) -> Tuple[str, List[str]]:
    """(domain, scope_tags) for a persona — deterministic, no questions asked."""
    low = f" {persona.strip().lower()} "
    for keys, domain, scope in ROLE_PACKS:
        if any(k in low for k in keys):
            return domain, list(scope)
    # unknown role: its own kebab-case domain (fully isolated) + the persona's
    # own content words as a seed scope; tags accumulate as it works
    words = [w for w in "".join(c if c.isalnum() or c.isspace() else " "
                                for c in low).split() if len(w) > 2]
    domain = "-".join(words[:3]) or "general"
    return domain, words or ["notes"]
