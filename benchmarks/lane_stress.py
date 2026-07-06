"""Lane serving under shared-store load — the numbers behind "will lanes
hold up when three integrations write into one file?"

One SQLite store, four profiled lanes (devops / security / backend /
content), REAL embedder (bge-small), facts seeded in-process, and every
measurement taken through the running daemon's HTTP path — the same path
the Claude Code / OpenClaw / Hermes adapters use.

Measures per configuration (at the default 5k cap, and 4x beyond it):
  - /search latency p50 / p95 / max, per-lane, 50 queries each
  - top-1 retrieval accuracy (the query's target fact comes back first)
  - /boot lane-snapshot latency
  - /turn write latency at scale
  - cross-lane leaks: lane A queried with lane B's vocabulary must not
    serve lane B's facts

Run:  ENGRAM_MAX_FACTS=25000 python benchmarks/lane_stress.py
"""
from __future__ import annotations

import json
import os
import random
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engram.core.memory import Memory  # noqa: E402
from engram import profiles  # noqa: E402

LANES = {
    "devops-bot": ("DevOps engineer", lambda i:
        f"Deploy note {i}: service svc-{i} rolls back through runbook RB-{i} "
        f"when the canary error rate passes {i % 9 + 1} percent"),
    "security-bot": ("security engineer", lambda i:
        f"Advisory {i}: CVE-2026-{10000 + i} in package pkg-{i} is mitigated "
        f"by rotating credential KEY-{i} and applying patch P-{i}"),
    "backend-bot": ("backend engineer", lambda i:
        f"Perf note {i}: endpoint /api/resource-{i} relies on index idx-{i} "
        f"to keep p99 query latency under {i % 40 + 5} ms"),
    "content-bot": ("content writer", lambda i:
        f"Draft {i}: article headline HL-{i} targets keyword kw-{i} and "
        f"publishes in week {i % 52 + 1}"),
}

QUERY_OF = {
    "devops-bot": lambda i: (f"how do we roll back svc-{i}?", f"RB-{i}"),
    "security-bot": lambda i: (f"how is CVE-2026-{10000 + i} mitigated?", f"KEY-{i}"),
    "backend-bot": lambda i: (f"what index does /api/resource-{i} use?", f"idx-{i}"),
    "content-bot": lambda i: (f"which keyword does article HL-{i} target?", f"kw-{i}"),
}

FOREIGN_MARKERS = {
    "devops-bot": ["kw-", "HL-", "KEY-", "idx-"],
    "security-bot": ["kw-", "HL-", "RB-", "idx-"],
    "backend-bot": ["kw-", "HL-", "RB-", "KEY-"],
    "content-bot": ["RB-", "KEY-", "idx-", "svc-"],
}


def seed(db: str, per_lane: int) -> float:
    t0 = time.time()
    for agent, (persona, fact_of) in LANES.items():
        domain, scope = profiles.derive_profile(persona)
        mem = Memory(db, origin_tool=agent)
        mem.register_profile(agent, persona, domain, scope)
        for i in range(per_lane):
            mem.remember(fact_of(i), tags=scope[:4])
        mem.close()
    return time.time() - t0


def call(state: dict, method: str, path: str, body=None, timeout=10.0):
    headers = {"Authorization": f"Bearer {state['token']}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}{path}", data=data,
        method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read())


def pct(values, p):
    return statistics.quantiles(values, n=100)[p - 1] if len(values) >= 2 else values[0]


def run_config(per_lane: int, label: str) -> dict:
    dir_ = tempfile.mkdtemp(prefix="engram-lane-stress-")
    db = os.path.join(dir_, "memory.db")

    seed_s = seed(db, per_lane)
    total = per_lane * len(LANES)
    db_mb = os.path.getsize(db) / 1e6

    proc = subprocess.Popen(
        [sys.executable, "-m", "engram.cli.main", "serve", "--db", db, "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    state = json.loads(proc.stdout.readline())["engram_serve"]

    try:
        rng = random.Random(7)
        search_ms, top1_hits, top1_total = [], 0, 0
        for agent in LANES:
            for _ in range(50):
                i = rng.randrange(per_lane)
                query, marker = QUERY_OF[agent](i)
                t0 = time.time()
                out = call(state, "POST", "/search",
                           {"query": query, "agent": agent, "k": 4})
                search_ms.append((time.time() - t0) * 1000)
                top1_total += 1
                hits = out["hits"]
                if hits and marker in hits[0]["value"]:
                    top1_hits += 1

        boot_ms = []
        for agent in LANES:
            for _ in range(10):
                t0 = time.time()
                call(state, "GET", f"/boot?agent={agent}&k=6")
                boot_ms.append((time.time() - t0) * 1000)

        write_ms = []
        for j in range(20):
            t0 = time.time()
            call(state, "POST", "/turn",
                 {"text": f"Live write {j}: operator confirmed checkpoint chk-{j} at scale"})
            write_ms.append((time.time() - t0) * 1000)

        leaks = 0
        leak_checks = 0
        for agent in LANES:
            for _ in range(25):
                i = rng.randrange(per_lane)
                foreign_agent = rng.choice([a for a in LANES if a != agent])
                query, _ = QUERY_OF[foreign_agent](i)
                out = call(state, "POST", "/search",
                           {"query": query, "agent": agent, "k": 4})
                leak_checks += 1
                for h in out["hits"]:
                    if any(m in h["value"] for m in FOREIGN_MARKERS[agent]):
                        leaks += 1
                        break
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    return {
        "label": label, "facts": total, "db_mb": db_mb, "seed_s": seed_s,
        "search_p50": pct(search_ms, 50), "search_p95": pct(search_ms, 95),
        "search_max": max(search_ms),
        "top1": 100.0 * top1_hits / top1_total,
        "boot_p50": pct(boot_ms, 50), "boot_p95": pct(boot_ms, 95),
        "write_p50": pct(write_ms, 50), "write_p95": pct(write_ms, 95),
        "leaks": leaks, "leak_checks": leak_checks,
    }


def main() -> int:
    print(f"embedder: real (default model) · MAX_FACTS={os.environ.get('ENGRAM_MAX_FACTS', '5000')}")
    rows = [
        run_config(per_lane=1250, label="at default cap (5k)"),
        run_config(per_lane=5000, label="4x beyond cap (20k)"),
    ]
    header = (f"{'config':<22}{'facts':>7}{'db MB':>7}{'seed s':>8}"
              f"{'search p50':>12}{'p95':>8}{'max':>8}{'top-1':>8}"
              f"{'boot p50':>10}{'write p50':>11}{'leaks':>10}")
    print(header)
    print("─" * len(header))
    for r in rows:
        print(f"{r['label']:<22}{r['facts']:>7}{r['db_mb']:>7.1f}{r['seed_s']:>8.0f}"
              f"{r['search_p50']:>10.1f}ms{r['search_p95']:>6.1f}ms{r['search_max']:>6.1f}ms"
              f"{r['top1']:>7.1f}%"
              f"{r['boot_p50']:>8.1f}ms{r['write_p50']:>9.1f}ms"
              f"{r['leaks']:>6}/{r['leak_checks']}")
    ok = all(r["leaks"] == 0 and r["search_p95"] < 1500 and r["top1"] >= 90 for r in rows)
    print()
    print("LANE STRESS: " + ("SERVING HOLDS" if ok else "ISSUES FOUND"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
