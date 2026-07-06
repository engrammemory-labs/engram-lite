"""UserPromptSubmit: capture the prompt, serve the lane.

One hook, both directions — the prompt text is written to memory (the
engine decides deterministically what to keep and ledgers why), and
memories relevant to THIS prompt are injected as additionalContext.
Slash commands and trivial fragments are skipped; a down daemon is
restarted detached and this prompt proceeds without memory.
"""
from __future__ import annotations

import json
import sys

try:
    from ._client import (clip, config, daemon_state, ensure_detached,
                          memory_block, read_stdin_event, request)
except ImportError:   # running as a plugin-dir script, not a package module
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    from _client import (clip, config, daemon_state, ensure_detached,
                         memory_block, read_stdin_event, request)

PROMPT_CHAR_CAP = 32_000
QUERY_CHAR_CAP = 600


def main() -> int:
    event = read_stdin_event()
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        return 0
    text = prompt.strip()
    if len(text) < 8 or text.startswith("/"):
        return 0

    cfg = config()
    state = daemon_state(cfg)
    if not state:
        ensure_detached(cfg)   # warm for the next prompt; this one goes without
        return 0

    captured = request(state, "POST", "/turn",
                       {"text": clip(text, PROMPT_CHAR_CAP), "speaker": "user"},
                       timeout=2.5)
    if captured is None:
        ensure_detached(cfg)   # stale state file: daemon died — revive it
        return 0

    found = request(state, "POST", "/search",
                    {"query": text[:QUERY_CHAR_CAP], "agent": str(cfg["agent"]),
                     "k": int(cfg["topK"])},
                    timeout=2.5)
    hits = [h.get("value", "") for h in (found or {}).get("hits", [])]
    block = memory_block([], hits)
    if block:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }}))
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
