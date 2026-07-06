"""Wiring the engram hooks into Claude Code's user settings.

The pip-install path: `engram claude setup` (with consent) merges three hook
entries into ~/.claude/settings.json so no plugin marketplace is needed.
Rules, in order of importance:

  - never lose anything: an exact backup is written before the first change,
    hooks from other tools are preserved verbatim, and an unparseable
    settings file aborts with instructions instead of being overwritten;
  - idempotent: re-running setup replaces our entries, never duplicates;
  - reversible: `engram claude uninstall` removes exactly our entries and
    nothing else.

Our entries are identified by their command shape (`… claude hook <event>`),
so uninstall works even if the engram binary moved between installs.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Tuple

HOOK_EVENTS: List[Tuple[str, str, int]] = [
    ("SessionStart", "session-start", 90),
    ("UserPromptSubmit", "user-prompt", 10),
    ("Stop", "stop", 30),
]

_MARKERS = tuple(f"claude hook {sub}" for _, sub, _ in HOOK_EVENTS)


def settings_path() -> str:
    return os.path.expanduser(
        os.environ.get("ENGRAM_CLAUDE_SETTINGS", "~/.claude/settings.json"))


def _is_ours(entry: Dict[str, Any]) -> bool:
    for hook in entry.get("hooks", []):
        command = str(hook.get("command", ""))
        if any(marker in command for marker in _MARKERS):
            return True
    return False


def _load(path: str) -> Dict[str, Any]:
    """Parse or die loudly — a corrupt settings file must never be clobbered."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)   # ValueError propagates: caller reports, never writes
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def _write(path: str, data: Dict[str, Any], make_backup: bool) -> str | None:
    backup = None
    if make_backup and os.path.exists(path):
        backup = f"{path}.engram-backup-{time.strftime('%Y%m%dT%H%M%S')}"
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.engram-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return backup


def wire(path: str, engram_cmd: str) -> Dict[str, Any]:
    """Merge our three hook entries into settings.json. Returns a summary."""
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('settings.json "hooks" is not an object')

    added = 0
    for event, subcommand, timeout in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f'settings.json hooks.{event} is not a list')
        entries[:] = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        entries.append({
            "hooks": [{
                "type": "command",
                "command": f"{engram_cmd} claude hook {subcommand}",
                "timeout": timeout,
            }],
        })
        added += 1

    backup = _write(path, data, make_backup=True)
    return {"added": added, "backup": backup, "path": path}


def unwire(path: str) -> Dict[str, Any]:
    """Remove exactly our entries; everything else stays byte-for-byte."""
    try:
        data = _load(path)
    except ValueError:
        return {"removed": 0, "path": path,
                "error": "settings.json is not valid JSON; nothing was changed"}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"removed": 0, "path": path}

    removed = 0
    for event in list(hooks.keys()):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)

    if removed:
        _write(path, data, make_backup=True)
    return {"removed": removed, "path": path}
