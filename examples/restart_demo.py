"""The core idea in one script: run it twice.

Run 1: two agents on this machine work and remember (one shared store).
Run 2 (a completely new process): each agent picks its memory back up, and the
SAME question serves each agent DIFFERENT memory, because serving is
conditioned on who the agent is (persona / domain / scope).

    python examples/restart_demo.py      # run it
    python examples/restart_demo.py      # run it again -> memory survived

Uses the offline stub embedder so it needs no model download.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENGRAM_EMBEDDER", "hash")   # offline demo, no downloads

from engram import Memory  # noqa: E402

DB = os.path.expanduser("~/.engram/restart-demo.db")
QUERY = "production incident: payments latency is spiking right after the deploy"
TASK = ["incident", "latency", "alert", "deploy", "rollback", "trace"]


def first_run(mem: Memory) -> None:
    print("=== run 1: agents work and remember (run me again afterwards) ===")
    mem.register_profile("oncall_sre", "on-call SRE", "sre-devops",
                         ["alert", "incident", "latency", "trace", "mitigation", "db"])
    mem.register_profile("devops_engineer", "DevOps engineer", "sre-devops",
                         ["deploy", "release", "rollback", "canary", "capacity", "nodes"])
    for text, tags in [
        ("Alert PAGE-99 fired: payments p99 latency at 2.3s", ["alert", "latency", "incident"]),
        ("Trace tr-77 shows db connection pool saturation", ["trace", "latency", "db", "incident"]),
        ("Mitigation: bump the db_pool env and rolling-restart the pods", ["mitigation", "db"]),
        ("Release v2.3.1 deployed at 14:02 via the canary pipeline", ["deploy", "release", "canary"]),
        ("Rollback runbook: argocd app rollback, then freeze deploys", ["rollback", "deploy"]),
    ]:
        mem.save(text, tags=tags)
    print("stored 5 memories across 2 agent profiles. Now run this script again.")


def second_run(mem: Memory) -> None:
    print("=== run 2 (fresh process): memory survived the restart ===")
    print(f"\nsame question to both agents: \"{QUERY}\"\n")
    for agent in ("oncall_sre", "devops_engineer"):
        prof = mem.profile(agent)
        print(f"--- {agent} ({prof['persona']}) is served: ---")
        for h in mem.search(QUERY, agent=agent, task_tags=TASK, k=3):
            lane = ", ".join(h["promotion"]["lane"])
            print(f"  - {h['value']}   [lane: {lane}]")
        print()
    print("Same store, same question, different memory per agent. That is conditioned")
    print("serving. (Delete the demo store with: rm ~/.engram/restart-demo.db)")


def main() -> None:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    mem = Memory(DB, origin_tool="restart_demo")
    if mem.profile("oncall_sre") is None:
        first_run(mem)
    else:
        second_run(mem)
    mem.close()


if __name__ == "__main__":
    main()
