# Engram Memory for Claude Code

Deterministic long-term memory for Claude Code, served from a local
daemon. Every prompt is captured, role-scoped memories are injected each
turn, and memory survives `/clear` and compaction — with a decision ledger
that explains every keep, merge, and drop. No cloud, no keys.

## How it works

```
Claude Code                                engram serve (loopback daemon)
┌──────────────────────────┐    HTTP      ┌───────────────────────────────┐
│ SessionStart hook        │  127.0.0.1   │ deterministic engine          │
│   → boot memory block    │───bearer────▶│ · role-scoped lanes           │
│ UserPromptSubmit hook    │    token     │ · zero-LLM promotion & recall │
│   → capture + serve      │              │ · decision ledger             │
│ Stop hook                │              │ · one SQLite store, shared    │
│   → capture output       │              │   across integrations         │
└──────────────────────────┘              └───────────────────────────────┘
```

Hooks are sub-second stdlib processes; the daemon does the work. The
daemon is found through a state file it maintains (`<db>.serve.json`) and
is started on demand by `engram serve --ensure` — idempotent, one daemon
per store, safe under concurrent callers.

- **SessionStart** (startup, resume, `/clear`, compaction): injects what
  this agent already knows. Context loss is exactly when memory matters.
- **UserPromptSubmit**: writes the prompt to memory (the engine decides
  what survives) and injects memories relevant to this prompt.
- **Stop**: captures the assistant's final message — drafts, decisions,
  conclusions.

Everything fails open: a slow or missing daemon means a session without
memory, never a stalled session.

## Install

```bash
pip install engram-lite    # the engine + the hooks (or uv tool / pipx)
engram claude setup        # one question, one consent — memory is live
```

Setup wires three hook entries into `~/.claude/settings.json` (with your
consent, a backup, and `engram claude uninstall` to reverse it). No
marketplace needed. Alternatives:

```bash
claude --plugin-dir path/to/claude-plugin        # try without touching settings
/plugin marketplace add engrammemory-labs/engram-lite   # plugin-native install
/plugin install engram@engram
```

## Configuration

`~/.engram/claude.json` (written by `engram claude setup`, all optional):

| Key | Default | Meaning |
| --- | --- | --- |
| `persona` | `software engineer` | One sentence; domain and memory scope are derived |
| `db` | `~/.engram/memory.db` | The shared store — point other integrations at the same file to share memory |
| `agent` | `claude-code` | Identity used for lane scoping and provenance |
| `topK` / `bootK` | `4` / `6` | Injection caps per prompt / per session boot |
| `engramCmd` | `engram` | Command used to ensure the daemon |

## Notes

- The store is shared by design: agents in other tools using the same db
  each get their own lane; cross-lane isolation is enforced by the engine.
- Memories are injected as recalled facts, never as instructions.
- `<db>.serve.log` holds the daemon's structured logs (rotated at 5 MB).

## Trust model

- Everything is same-user local: the store, its WAL, and the daemon state
  file are created 0600, and the daemon binds 127.0.0.1 with a per-boot
  bearer token. Processes running as *other* users can reach none of it; a
  malicious process running as *you* is outside this threat model (it
  already owns your session).
- Hooks verify a daemon (pid + tokened health + store identity) before any
  text leaves the process, and fail open — a down daemon means a session
  without memory, never a stalled or broken one.
- Recalled text is data, not instructions: the injected block says so
  explicitly, and injection is bounded. Anything a memory says to *do*
  should be treated with the same skepticism as any other quoted content.
- Platform scope: macOS and Linux (hooks invoke `python3`). Windows support
  is planned, not claimed.
