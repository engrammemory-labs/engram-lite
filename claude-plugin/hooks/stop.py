"""Stop: capture what the assistant produced this turn.

The event delivers last_assistant_message directly — no transcript parsing.
Assistant output is where drafts, decisions, and conclusions live; the
engine's extraction and junk filters decide what survives, and the ledger
records why. Never blocks the stop (no decision field is ever emitted).
"""
from __future__ import annotations

import sys

try:
    from ._client import clip, config, daemon_state, ensure_detached, read_stdin_event, request
except ImportError:   # running as a plugin-dir script, not a package module
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    from _client import clip, config, daemon_state, ensure_detached, read_stdin_event, request

MESSAGE_CHAR_CAP = 32_000


def main() -> int:
    event = read_stdin_event()
    message = event.get("last_assistant_message")
    if not isinstance(message, str) or len(message.strip()) < 40:
        return 0

    cfg = config()
    state = daemon_state(cfg)
    if not state:
        ensure_detached(cfg)
        return 0

    request(state, "POST", "/turn",
            {"text": clip(message.strip(), MESSAGE_CHAR_CAP), "speaker": "assistant"},
            timeout=8)
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
