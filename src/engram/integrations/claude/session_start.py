"""SessionStart: boot memory into the session — including after /clear and
compaction, which is exactly when context was lost and memory matters most.

Plain stdout on exit 0 becomes context for this event, so no JSON envelope
is needed. Prints nothing when the lane is empty (abstention) or the daemon
is unavailable (fail open).
"""
from __future__ import annotations

import sys

try:
    from ._client import config, ensure_blocking, memory_block, read_stdin_event, request
except ImportError:   # running as a plugin-dir script, not a package module
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    from _client import config, ensure_blocking, memory_block, read_stdin_event, request


def main() -> int:
    event = read_stdin_event()
    cfg = config()

    # a fresh machine may need the model download; a warm daemon answers in
    # milliseconds. 45s keeps first-ever startup usable without gating every
    # later session on the worst case.
    state = ensure_blocking(cfg, timeout_s=45)
    if not state:
        return 0

    # idempotent: (re-)register this agent's persona so lanes exist day 0
    request(state, "POST", "/profile",
            {"persona": str(cfg["persona"]), "agent": str(cfg["agent"])}, timeout=5)

    boot = request(state, "GET",
                   f"/boot?agent={cfg['agent']}&k={int(cfg['bootK'])}", timeout=5)
    block = memory_block(boot.get("memories", []) if boot else [], [])
    if block:
        source = str(event.get("source", "startup"))
        if source in ("clear", "compact"):
            block += f"\n(Restored after {source}: long-term memory persists.)"
        print(block)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # a memory hook must never surface a traceback into the user's
        # session or block it with a nonzero exit — fail open, stay silent
        raise SystemExit(0)
