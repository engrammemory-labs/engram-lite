# engram — Hermes memory plugin

Conditioned local memory for Hermes agents: each profile is served the memory
that fits its **persona, domain, and task** (the lane model), with honest
abstention when a topic is outside its lane, hard domain isolation between
profiles sharing one store, and restart-proof recall. Local SQLite, no API
keys, no network — the engine never calls an LLM.

## Install

```bash
# 1. the plugin (user plugin dir — no changes to hermes-agent itself)
mkdir -p ~/.hermes/plugins
cp -R hermes-plugin/engram ~/.hermes/plugins/

# 2. the engine, into the SAME python environment that runs hermes
pip install engram-lite            # or: pip install /path/to/engram-lite

# 3. activate (config.yaml)
#      memory:
#        provider: engram

# 4. optional — conditioned serving (persona / domain / scope):
hermes memory setup
# or write ~/.hermes/engram/config.json:
#   {"persona": "on-call SRE", "domain": "sre-devops",
#    "scope_tags": "alert,incident,latency,trace,mitigation,db"}
```

Without persona/domain/scope the plugin still gives persistent, self-cleaning,
restart-proof memory (plain multi-signal recall). With them, serving becomes
conditioned — the differentiator.

## What Hermes gets

| Hermes lifecycle | engram behavior |
|---|---|
| session start | lane snapshot injected into the system prompt (restart pickup) |
| before each turn | `prefetch` — conditioned recall, milliseconds, deterministic |
| model tools | `memory_search` / `memory_write` (flat OpenAI schemas, JSON results) |
| after each turn | gated auto-capture (stands down when the model uses the tools) |
| built-in memory writes | mirrored into the store (`on_memory_write`) |
| **before context compression** | salient facts captured from the messages being discarded (`on_pre_compress`) — nothing durable is lost to compaction |
| cron / subagent contexts | writes refused — background jobs can't corrupt the store |
| `hermes backup` | covered: the store lives under `HERMES_HOME/engram/` |

## Verify against your hermes install

```bash
PYTHONPATH=~/.hermes/hermes-agent ENGRAM_EMBEDDER=hash python verify.py
```

Runs 13 checks driving the provider exactly as Hermes's MemoryManager does
(real ABC, real call shapes, restart pickup, context guards). No LLM, no key.
