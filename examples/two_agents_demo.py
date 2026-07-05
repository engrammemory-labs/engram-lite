"""N Hermes-style agents, one shared memory, real restarts.

Runs ONE agent for ONE session as a single OS process, so a "restart" is a real
process death, not a simulation:

    python examples/two_agents_demo.py --agent security_engineer --session 1 --db /tmp/m.db
    python examples/two_agents_demo.py --agent security_engineer --session 2 --db /tmp/m.db
    python examples/two_agents_demo.py --agent security_engineer --probe-only --db /tmp/m.db
    python examples/two_agents_demo.py --inspect --db /tmp/m.db

Session 1: the agent does real work (real Claude calls; the model can call
memory_search / memory_write as tools) and every turn is auto-captured through
the salience gate. Session 2: a fresh process boots, receives its session-start
memory block, answers the group question from memory, then runs deterministic
serving probes (conditioned recall, abstention, cross-domain leak checks).
--probe-only runs just the deterministic probes (no LLM, no key needed).

Profiles span FOUR domains on purpose — tag collisions across them (policy,
db, training, onboarding) are deliberate: the domain gate must keep serving
separated. Needs ANTHROPIC_API_KEY for sessions (not for --inspect/--probe-only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from engram import Memory
from engram.integrations.hermes import EngramMemoryProvider

MODEL = "claude-haiku-4-5-20251001"

PROFILES = {
    # ── phase-1 pair ──────────────────────────────────────────────────────────
    "oncall_sre": dict(
        persona="on-call SRE", domain="sre-devops",
        scope_tags=["alert", "incident", "latency", "trace", "mitigation", "db", "oncall"],
    ),
    "devops_engineer": dict(
        persona="DevOps engineer", domain="sre-devops",
        scope_tags=["deploy", "release", "rollback", "canary", "capacity", "nodes",
                    "change-mgmt", "k8s", "upgrade", "ingress"],
    ),
    # ── tech trio ─────────────────────────────────────────────────────────────
    "security_engineer": dict(
        persona="security engineer", domain="security-engineering",
        scope_tags=["security", "vulnerability", "cve", "patch", "secrets", "rotation",
                    "auth", "access", "audit", "compliance", "policy"],
    ),
    "backend_engineer": dict(
        persona="backend engineer", domain="backend-engineering",
        scope_tags=["api", "endpoints", "db", "queries", "schema", "migrations",
                    "index", "cache", "idempotency", "latency"],
    ),
    # ── non-tech trio ─────────────────────────────────────────────────────────
    "recruiter": dict(
        persona="technical recruiter", domain="talent-acquisition",
        scope_tags=["hiring", "pipeline", "interview", "offer", "candidate",
                    "headcount", "sourcing", "referral", "joining"],
    ),
    "product_manager": dict(
        persona="product manager", domain="product-mgmt",
        scope_tags=["roadmap", "feature", "launch", "metrics", "conversion",
                    "experiment", "feedback", "pricing", "users", "checkout"],
    ),
    "hr_partner": dict(
        persona="HR business partner", domain="people-ops",
        scope_tags=["benefits", "policy", "leave", "insurance", "payroll",
                    "onboarding", "grievance", "wellness", "training"],
    ),
    # ── wave 3: support / frontend / data-science ─────────────────────────────
    "support_engineer": dict(
        persona="support engineer", domain="customer-support",
        scope_tags=["ticket", "customer", "escalation", "refund", "sla",
                    "complaint", "resolution", "workaround", "otp"],
    ),
    "frontend_engineer": dict(
        persona="frontend engineer", domain="frontend-engineering",
        scope_tags=["ui", "components", "bundle", "css", "react",
                    "accessibility", "forms", "checkout", "hydration"],
    ),
    "data_scientist": dict(
        persona="data scientist", domain="data-science",
        scope_tags=["model", "dataset", "training", "evaluation", "experiment",
                    "pipeline", "accuracy", "drift", "fraud"],
    ),
    # ── wave 4: knowledge-work agents (deliberately blurry personas) ──────────
    "chief_of_staff": dict(
        persona="chief of staff", domain="exec-ops",
        scope_tags=["priorities", "okrs", "decisions", "board", "stakeholders",
                    "meetings", "investor-updates", "follow-ups"],
    ),
    "coach": dict(
        persona="executive coach", domain="coaching",
        scope_tags=["goals", "habits", "feedback", "development", "wellbeing",
                    "burnout", "one-on-one", "delegation"],
    ),
    "content_writer": dict(
        persona="content writer", domain="content",
        scope_tags=["blog", "copy", "tone", "style", "headlines", "publishing",
                    "calendar", "launch", "seo"],
    ),
    "startup_researcher": dict(
        persona="startup researcher", domain="startup-research",
        scope_tags=["competitors", "funding", "founders", "product-launches",
                    "positioning", "whitespace"],
    ),
    "market_researcher": dict(
        persona="market researcher", domain="market-research",
        scope_tags=["market-size", "trends", "segments", "surveys", "pricing",
                    "tam", "analysts"],
    ),
}

SESSION1_TURNS = {
    "oncall_sre": [
        "PAGE-99 just fired: payments-api p99 latency is 2.3s and the error rate is 4%. "
        "Acknowledge and tell me your first diagnostic step.",
        "Trace tr-77 shows the DB connection pool at 100% saturation. We bumped the db_pool "
        "env to 40 and rolling-restarted the pods; latency recovered at 14:25, right after "
        "the v2.3.1 deploy. Make sure this is noted for the future.",
    ],
    "devops_engineer": [
        "We're upgrading the payments EKS cluster from 1.29 to 1.31 next week. Plan: canary "
        "nodepool first, then the rolling upgrade. There's a deploy freeze on Friday. Note this.",
        "Incident this morning: ingress 502s right after the cert rotation. The fix was "
        "rolling the ingress pods. Note the fix for next time.",
        "For tomorrow's payments release: canary at 10% for 30 minutes, auto-rollback if "
        "errors exceed 2%. Remember.",
    ],
    "security_engineer": [
        "Critical CVE-2026-4412 in the openssl base image. The fleet must be patched within "
        "72 hours per our security policy, and tomorrow's payments release must ship on the "
        "patched image. Note it.",
        "We're moving all DB credentials to Vault. Rotation policy is quarterly, and the old "
        "static creds get revoked on Friday. Record this.",
        "Audit finding: three service accounts still have cluster-admin. They must be scoped "
        "down before the next compliance review. Note.",
    ],
    "backend_engineer": [
        "We fixed the N+1 query on the orders list endpoint with a batched IN-query. p99 "
        "went from 450ms to 80ms. Note the fix.",
        "Decision: POST /refunds requires an Idempotency-Key header and duplicates return "
        "the original response. This ships in tomorrow's payments release. Remember this.",
        "Migration 0043 adds a partial index on orders(status). It must run before the "
        "release traffic ramps. Note.",
    ],
    "recruiter": [
        "Five backend engineers accepted offers and join on August 4th as one cohort. Note it.",
        "The hiring loop for the platform role is now 4 stages: screen, coding, system "
        "design, values. Record.",
        "Candidate Priya cleared the onsite for the SRE role; references pending and the "
        "offer is targeted for Friday. Note.",
    ],
    "product_manager": [
        "Q3 roadmap: the checkout revamp ships behind a feature flag with a target of +2% "
        "conversion. Note the plan.",
        "User feedback analysis: 18% drop-off at the OTP step. We're planning an experiment "
        "with resend-timer changes. Record it.",
        "The checkout revamp needs two of the new engineers joining next month, otherwise "
        "the launch slips to September. Note this dependency.",
    ],
    "hr_partner": [
        "The parental leave policy is updated to 26 weeks, effective August 1st. Note the "
        "policy change.",
        "Health insurance renewal is done: the sum insured doubled at the same premium for "
        "employees. Record it.",
        "The onboarding checklist now includes security training on day one for every new "
        "joiner. Note.",
    ],
    "support_engineer": [
        "Ticket SUP-291: enterprise customer Acme reports duplicate refund charges. It's "
        "escalated to backend; the workaround is a manual reversal within 24 hours. Note it.",
        "Our SLA for enterprise tickets is first response in 2 hours and resolution in 24 "
        "hours. Record.",
        "The top complaint this week is the OTP not arriving. Workaround: resend after 60 "
        "seconds. The permanent fix is owned by product. Note.",
    ],
    "frontend_engineer": [
        "For the checkout revamp UI: the new payment form component ships behind the same "
        "feature flag, and the bundle size must stay under 250KB. Note.",
        "Accessibility audit: the OTP input fails screen-reader labels. The fix is "
        "scheduled for this sprint. Record.",
        "We upgraded to React 19 last week and the hydration warnings are gone. Note.",
    ],
    "data_scientist": [
        "The fraud model v3 was retrained on June data: precision 0.91, recall 0.84. It "
        "ships to shadow mode on Monday. Note.",
        "Training pipeline: the feature store refresh runs daily at 02:00 UTC and drift "
        "alerts go to the ml-alerts channel. Record.",
        "Experiment result: setting the checkout-risk score threshold at 0.7 cuts false "
        "declines by 12 percent with flat fraud loss. Note this result.",
    ],
    "chief_of_staff": [
        "The board meeting moved to July 15. The deck needs the Q2 metrics and the hiring "
        "plan. Note it.",
        "Decision from the leadership sync: we pause the EU expansion until Q4 and revisit "
        "after the funding close. Record.",
        "Standing follow-ups I own: CEO one-on-one every Monday 9am, and the investor "
        "update goes out the first Friday of each month. Note.",
    ],
    "coach": [
        "From today's session: Arjun's goal is to delegate more. The habit we agreed: no "
        "code reviews after 8pm, with a weekly check-in. Note.",
        "Feedback theme from the team survey: decisions are fast but the context behind "
        "them isn't shared. The coaching focus is narrating the why. Record.",
        "Wellbeing flag: two engineers show burnout signs. Recommendation is no-meeting "
        "Fridays for the next four weeks. Note.",
    ],
    "content_writer": [
        "The launch blog post for our payments SDK: tone is builder-to-builder, no "
        "marketing fluff. Headline draft: ship payments in an afternoon. Note.",
        "Style rule for everything we publish: plain words, short sentences, no jargon. "
        "Record this.",
        "Content calendar: the benchmark deep-dive publishes Tuesday and the "
        "integration guide goes out Thursday. Note.",
    ],
    "startup_researcher": [
        "Competitor scan: PayForge raised a Series A and launched a managed platform. "
        "Their pitch is checkout in one API with no fraud tooling. Note.",
        "LedgerKit pivoted messaging to embedded finance, and SwiftRails is pushing the "
        "banking-as-a-service framing. Record.",
        "Positioning gap: nobody in the scan bundles fraud scoring with payouts. That "
        "is open whitespace for us. Note this.",
    ],
    "market_researcher": [
        "Market sizing: analysts put agent infrastructure at 6 billion dollars by 2027, "
        "and memory tooling is the fastest growing slice. Note.",
        "Survey of 40 agent builders: 70 percent hand-roll memory on pgvector, and the "
        "top complaint is irrelevant memory being served. Record.",
        "Pricing signal: builders pay 20 to 50 dollars a month for memory tooling, while "
        "enterprises expect seat-based pricing. Note.",
    ],
}

TECH = {"oncall_sre", "devops_engineer", "security_engineer", "backend_engineer"}
WAVE3 = {"support_engineer", "frontend_engineer", "data_scientist"}
WAVE4 = {"chief_of_staff", "coach", "content_writer", "startup_researcher",
         "market_researcher"}


def session2_question(agent: str) -> str:
    if agent in TECH:
        return ("We are shipping the payments release tomorrow. From your side, what do "
                "we already know that matters for it?")
    if agent in WAVE3:
        return ("The payments release ships tomorrow and five new engineers join next "
                "month. From your side, what do we already know that matters?")
    if agent in WAVE4:
        return ("We are preparing the payments SDK public launch next week. From your "
                "side, what do we already know that matters?")
    return ("Five new engineers join next month as part of the growth plan. From your "
            "side, what do we already know that matters?")

# deterministic serving probes: (label, query, task_tags)
PROBES = {
    "oncall_sre": [
        ("in-lane", "payments latency incident history", ["incident", "latency", "trace"]),
        ("out-of-lane", "hiring pipeline status", ["hiring", "offer"]),
    ],
    "devops_engineer": [
        ("in-lane", "payments release readiness", ["deploy", "canary", "rollback"]),
        ("out-of-lane", "hiring pipeline for the platform role", ["hiring", "offer"]),
    ],
    "security_engineer": [
        ("in-lane", "what patching deadline applies to the release", ["cve", "patch", "policy"]),
        ("collision:db-wording", "database credentials rotation status", ["secrets", "rotation"]),
        ("LEAK-CHECK:policy", "what policies must we follow", ["policy", "compliance"]),
    ],
    "backend_engineer": [
        ("in-lane", "orders endpoint performance and pending migrations",
         ["db", "queries", "latency", "migrations"]),
        ("LEAK-CHECK:db-tag", "db work in flight", ["db"]),
    ],
    "recruiter": [
        ("in-lane", "who is joining and when", ["hiring", "offer", "joining", "candidate"]),
        ("out-of-lane", "deployment plan for the release", ["deploy", "canary"]),
    ],
    "product_manager": [
        ("in-lane", "checkout plans, experiments and metrics",
         ["roadmap", "experiment", "conversion", "checkout"]),
        ("out-of-lane", "leave policy details", ["benefits", "leave"]),
    ],
    "hr_partner": [
        ("in-lane", "what changed in policies, benefits and onboarding",
         ["policy", "benefits", "onboarding", "training"]),
        ("out-of-lane", "release rollback procedure", ["deploy", "rollback"]),
    ],
    "support_engineer": [
        ("in-lane", "open escalations and SLAs", ["ticket", "escalation", "sla"]),
        ("LEAK-CHECK:refund", "refund handling", ["refund"]),
        ("out-of-lane", "deployment plan", ["deploy", "canary"]),
    ],
    "frontend_engineer": [
        ("in-lane", "checkout UI state and audits", ["ui", "components", "bundle", "accessibility"]),
        ("LEAK-CHECK:checkout", "checkout work in flight", ["checkout"]),
        ("out-of-lane", "patching deadlines", ["cve", "patch"]),
    ],
    "data_scientist": [
        ("in-lane", "model status and results", ["model", "training", "evaluation", "fraud"]),
        ("LEAK-CHECK:experiment", "experiments in flight", ["experiment"]),
        ("out-of-lane", "leave and benefits", ["benefits", "leave"]),
    ],
    "chief_of_staff": [
        ("in-lane", "decisions, board and follow-ups", ["decisions", "board", "follow-ups"]),
        ("out-of-lane", "team development goals", ["goals", "habits"]),
    ],
    "coach": [
        ("in-lane", "coaching goals and wellbeing", ["goals", "habits", "wellbeing", "burnout"]),
        ("LEAK-CHECK:feedback", "feedback themes", ["feedback"]),
        ("out-of-lane", "board schedule", ["board", "investor-updates"]),
    ],
    "content_writer": [
        ("in-lane", "what we publish and how", ["blog", "tone", "style", "calendar"]),
        ("LEAK-CHECK:launch", "launch work in flight", ["launch"]),
        ("out-of-lane", "market sizing", ["market-size", "tam"]),
    ],
    "startup_researcher": [
        ("in-lane", "competitor moves and positioning", ["competitors", "positioning", "whitespace"]),
        ("out-of-lane", "market sizing and surveys", ["market-size", "surveys"]),
    ],
    "market_researcher": [
        ("in-lane", "market size, surveys and trends", ["market-size", "surveys", "trends"]),
        ("LEAK-CHECK:pricing", "pricing intelligence", ["pricing"]),
        ("out-of-lane", "competitor positioning", ["competitors", "positioning"]),
    ],
}


def log(kind: str, msg: str) -> None:
    print(f"[{kind}] {msg}", flush=True)


def agent_loop(client, provider: EngramMemoryProvider, system: str, user_text: str) -> str:
    """One user turn: real Claude call with the memory tools; returns the final text."""
    messages = [{"role": "user", "content": user_text}]
    tools = provider.get_tool_schemas()
    for _round in range(5):
        resp = client.messages.create(model=MODEL, max_tokens=600, temperature=0,
                                      system=system, tools=tools, messages=messages)
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        texts = [b.text for b in resp.content if b.type == "text"]
        if not tool_uses:
            return "\n".join(texts).strip()
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            log("tool_call", f"{tu.name} {json.dumps(tu.input)[:150]}")
            out = provider.handle_tool_call(tu.name, tu.input)
            log("tool_result", out[:180].replace("\n", " | "))
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "(agent hit the tool-round cap)"


def run_probes(provider: EngramMemoryProvider, agent: str) -> None:
    for label, query, task_tags in PROBES[agent]:
        served = provider.prefetch(query, task_tags=task_tags)
        log("probe", f"{label} · \"{query}\" tags={task_tags}")
        log("served", served.replace("\n", " ⏐ ") if served else "(nothing — abstained)")


def run_session(agent: str, session: int, db: str) -> None:
    import anthropic
    client = anthropic.Anthropic()
    p = PROFILES[agent]
    provider = EngramMemoryProvider(db_path=db, agent=agent, top_k=4, **p).initialize()

    block = provider.system_prompt_block()
    log("boot", f"{agent} pid={os.getpid()} session={session}")
    log("system_prompt_block", block if block else "(empty — no prior memory)")

    system = (f"You are the {p['persona']} on the payments team. Be brief and concrete. "
              f"Use memory_search before answering when past context would help; use "
              f"memory_write to store durable facts and decisions.\n\n{block}")

    if session == 1:
        for i, turn in enumerate(SESSION1_TURNS[agent], 1):
            log("turn", f"--- user turn {i} ---")
            log("user", turn[:150].replace("\n", " ⏎ "))
            answer = agent_loop(client, provider, system, turn)
            log("assistant", answer[:250].replace("\n", " | "))
            counts = provider.sync_turn(user_text=turn, assistant_text=answer)
            log("capture", f"auto-capture: {counts}")
    else:
        q = session2_question(agent)
        log("turn", "--- group question (memory test) ---")
        log("user", q)
        answer = agent_loop(client, provider, system, q)
        log("assistant", answer[:350].replace("\n", " | "))
        run_probes(provider, agent)

    provider.shutdown()
    log("exit", f"{agent} session {session} done, process exits")


def probe_only(agent: str, db: str) -> None:
    p = PROFILES[agent]
    provider = EngramMemoryProvider(db_path=db, agent=agent, top_k=4, **p).initialize()
    log("boot", f"{agent} probe-only pid={os.getpid()}")
    run_probes(provider, agent)
    provider.shutdown()


def inspect(db: str) -> None:
    os.environ.setdefault("ENGRAM_EMBEDDER", "hash")   # inspection needs no model
    m = Memory(db)
    facts = m.all_current()
    by_agent: dict = {}
    for f in facts:
        by_agent.setdefault(f["origin_tool"], []).append(f)
    print(f"── store dump: {db} ──")
    for agent, rows in sorted(by_agent.items()):
        untagged = sum(1 for r in rows if not r["tags"])
        print(f"\n  ▸ {agent}: {len(rows)} facts ({untagged} untagged)")
        for f in rows:
            print(f"    [{f['id'][:8]}] domain={f['domain']} tags={f['tags']}\n"
                  f"             {f['value'][:100]}")
    st = m.stats()
    print(f"\n── {st['facts_current']} current / {st['facts_total']} total · "
          f"{st['db_bytes'] // 1024} KB ──")
    for p in m.profiles():
        print(f"  profile: {p['agent']} = {p['persona']} / {p['domain']}")
    m.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=list(PROFILES))
    ap.add_argument("--session", type=int, choices=[1, 2])
    ap.add_argument("--db", required=True)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--probe-only", action="store_true")
    args = ap.parse_args()
    if args.inspect:
        inspect(args.db)
        return
    if args.probe_only:
        if not args.agent:
            ap.error("--probe-only needs --agent")
        probe_only(args.agent, args.db)
        return
    if not (args.agent and args.session):
        ap.error("--agent and --session are required (or use --inspect / --probe-only)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is required for a session run")
    run_session(args.agent, args.session, args.db)


if __name__ == "__main__":
    main()
