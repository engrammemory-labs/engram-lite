"""`engram demo` — an interactive REPL to see the engine work (no AI tool needed).

For each line you type: FIND relevant memories, then SAVE the salient ones.
"""
from __future__ import annotations

import argparse

from ..core.memory import Memory


def run(args: argparse.Namespace) -> int:
    mem = Memory(path="engram_demo.db")
    print("engram demo — type a message, or :quit. (:help for commands)\n")
    pinned_subject = None
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in (":quit", ":q"):
            break
        if line in (":help", ":h"):
            print("  :list  :forget <id>  :subject <name>  :quit")
            continue
        if line == ":list":
            for f in mem.all_current():
                print(f"  [{f['id'][:8]}] ({f['block_id']}) {f['value']}")
            continue
        if line.startswith(":forget "):
            mem.forget(line.split(" ", 1)[1].strip())
            print("  forgotten.")
            continue
        if line.startswith(":subject "):
            pinned_subject = line.split(" ", 1)[1].strip()
            print(f"  subject pinned: {pinned_subject}")
            continue

        hits = mem.search(line, k=3)
        if hits:
            print("  🧠 recalled:")
            for h in hits:
                print(f"     - {h['value']}")
        else:
            print("  🧠 (nothing relevant remembered yet)")
        res = mem.remember(line, subject=pinned_subject)   # gated save (salience)
        if res["decision"] == "SKIP":
            print(f"  · skipped — {res['reason']}")
        else:
            print(f"  💾 {res['decision']} → block '{res['block_id']}'")
        print()
    mem.close()
    return 0


def add_parser(sub) -> None:
    sub.add_parser("demo", help="interactive demo REPL").set_defaults(func=run)
