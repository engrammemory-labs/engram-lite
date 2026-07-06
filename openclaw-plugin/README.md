# Engram Memory for OpenClaw

Deterministic long-term memory for OpenClaw agents, served from a local
sidecar. No cloud calls, no embedding API, no keys — the memory layer runs
entirely on this machine and can explain every decision it makes.

## How it works

```
OpenClaw (Node)                         engram serve (Python, loopback only)
┌─────────────────────────┐   HTTP     ┌──────────────────────────────────┐
│ @engrammemorylabs/      │ 127.0.0.1  │ deterministic engine             │
│ openclaw-engram         │──bearer───▶│ · role-scoped lanes              │
│ · sidecar supervisor    │   token    │ · zero-LLM promotion & recall    │
│ · prompt-build recall   │            │ · decision ledger (audit trail)  │
│ · agent-end capture     │            │ · SQLite store, WAL              │
│ · memory tools          │            └──────────────────────────────────┘
└─────────────────────────┘
```

The plugin spawns and supervises `engram serve`, injects role-scoped
memories before each prompt, captures durable facts from user messages
after each run, and exposes three tools. All memory decisions are made by
the engine and recorded in a ledger — the plugin adds no hidden heuristics.

## Install

```bash
# 1. the engine
pip install engram-lite        # or: uv tool install engram-lite / pipx install engram-lite

# 2. the plugin
openclaw plugins install @engrammemorylabs/openclaw-engram
```

The plugin finds the engine via `pythonPath` (authoritative when set, also
honors `ENGRAM_PYTHON`) or `engram` on PATH. It never downloads anything on
its own; set `allowDownload: true` if you want uvx/pipx to fetch the
version-pinned release automatically.

## Configure

One question: what is this agent? Everything else has defaults.

```bash
openclaw engram setup      # asks the one question, proves the engine end to
                           # end, and writes the config via the host's own
                           # `openclaw config set`
openclaw engram status     # engine health, store stats, last ledger decisions
```

Or configure by hand:

```jsonc
{
  "plugins": {
    "slots": { "memory": "engram" },
    "entries": {
      "engram": {
        "enabled": true,
        "config": {
          "persona": "DevOps engineer"
        }
      }
    }
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `persona` | — (required) | One sentence describing the agent; domain and memory scope are derived deterministically |
| `dbPath` | `~/.engram/openclaw.db` | SQLite store; point several integrations at one file to share memory |
| `agentId` | `openclaw` | Identity used for lane scoping and provenance |
| `autoRecall` | `true` | Inject role-scoped memories before each prompt |
| `autoCapture` | `true` | Capture durable facts from user messages after each run |
| `topK` / `bootK` | `4` / `6` | Injection caps: per-prompt search hits / once-per-session lane snapshot |
| `pythonPath` | auto | Explicit interpreter with engram-lite installed (authoritative when set) |
| `allowDownload` | `false` | Opt-in only: let uvx/pipx fetch the version-pinned engine when it is not installed. Off by default — the plugin never downloads anything on its own |

## Tools

- **`memory_search`** — role-scoped lookup; an empty result means memory
  abstained rather than guessed.
- **`memory_store`** — save a fact; the engine decides add/merge/skip
  deterministically and records why.
- **`memory_diagnose`** — the decision ledger: every keep, merge, drop,
  truncation, and abstention, with the rule that fired.

## Design guarantees

- **Local only.** The sidecar binds `127.0.0.1` and refuses anything else;
  requests carry a per-boot bearer token. Memory itself has zero egress —
  no embedding API, no cloud, no keys. The plugin never initiates a network
  connection either, unless you explicitly set `allowDownload: true`, in
  which case uvx/pipx may fetch the version-pinned engine release once.
- **Never in the way.** Recall runs under a hard time budget and fails open;
  a slow or down sidecar means an agent without memory, never a stalled
  agent. Capture is resumable, not lossy: cursors advance only over messages
  the engine actually received, overflow and outages resume on the next run,
  and the engine's merge rules make redelivery safe.
- **Explainable.** Every memory decision — keep, merge, skip, truncate,
  serve, abstain — is made by the engine and recorded in its ledger;
  `memory_diagnose` shows the rule that fired. Prompt injection additionally
  applies visible display bounds (`topK`/`bootK`, line clipping with `…`,
  an explicit `+N more` marker); anything bounded there remains stored
  intact and reachable via `memory_search`.
- **No native bindings.** The Node side is dependency-free TypeScript; the
  engine is a separate process. Nothing here compiles against your Node
  version.
- **Supervised, not just spawned.** Crashed or failed-to-start sidecars are
  retried on a capped backoff budget; hard kills signal the whole process
  group so no orphan daemon is ever left holding the store.

## Development

```bash
npm run typecheck          # tsc --noEmit
npm test                   # node --test, runs against the real daemon
# tests need an interpreter with engram-lite; override with:
ENGRAM_TEST_PYTHON=/path/to/python npm test
```
