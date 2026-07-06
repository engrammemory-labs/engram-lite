# engram-lite

**Local memory for AI agents, served by persona, domain, and task. Survives restarts. One machine, many agents.**

Most agent memory is a shared pile: every agent gets the same top-k for the same query. In our benchmark (methodology below), an agent fed flat shared memory **hallucinated more than an agent given no memory at all.** Serving each agent the memory that fits **who it is** (its persona, its domain, the task in front of it) **doubled task success (40% to 80%)** and **cut hallucination to a third.**

And the memory layer never calls an LLM. On **LoCoMo** — the long-conversation memory benchmark — engram-lite scores **J 68.3** with **zero LLM calls and $0 to build the memory**. The protocol, footnotes, and the full comparison against the leading LLM-based memory systems live in [`benchmarks/locomo/`](https://github.com/engrammemory-labs/engram-lite/blob/main/benchmarks/locomo/README.md).

engram-lite is that serving layer, small enough to run on your laptop:

- **Conditioned serving.** Register a profile per agent (persona / domain / scope). The same question serves your on-call SRE agent the alert, the trace, and the mitigation runbook, and serves your release agent the deploy, the rollback, and the freeze policy. Out-of-lane questions correctly serve nothing.
- **Restart-proof.** Everything is stored in a single SQLite file. Your agent can restart tomorrow and pick up exactly where it left off.
- **Self-cleaning.** A salience gate skips junk (code, command output, questions). New facts are de-duplicated, updates supersede old versions, stale facts expire, and the store stays bounded.
- **Zero infrastructure.** No server, no cloud, no accounts, no telemetry. SQLite plus a local embedding model. `pip install` and you are running.
- **Explainable forgetting.** Every drop, merge, truncation, and abstention is recorded with the rule that fired (`Memory.decisions()`). "Why don't you remember X?" has an answer — something an LLM-written memory can't give you, because its keep/drop decisions live inside model weights.
- **Deterministic and replayable.** Same transcript in, same memory out — byte-identical across rebuilds. Memory failures can be diffed, bisected, and regression-gated like any other bug.
- **Plugs into what you build with.** A Hermes memory provider, a Claude Code plugin, an OpenClaw plugin, or three lines of Python — all sharing one store, each agent served its own lane.

> engram-lite is single-machine by design. A team/hosted edition, **engram-core**, is in development — reach out if that is what you need.

## Install

```bash
pip install engram-lite
```

On a Mac with Homebrew Python (`externally-managed-environment` error), use `pipx install engram-lite` or a virtualenv. Installing for a Hermes agent? Skip this — `hermes memory setup` installs it into Hermes's own environment automatically.

Or Docker (memory persists in the named volume):

```bash
docker build -t engram-lite .
docker run -it --rm -v engram-data:/data engram-lite demo
```

## 60 seconds: see conditioned memory work

```bash
python examples/restart_demo.py    # two agents remember
python examples/restart_demo.py    # run it AGAIN: memory survived, and the same
                                   # question serves each agent different memory
```

## Use it from Python

```python
from engram import Memory

mem = Memory("~/.engram/memory.db", origin_tool="oncall_sre")

# who is this agent? (this is what makes serving conditioned)
mem.register_profile(
    "oncall_sre", persona="on-call SRE", domain="sre-devops",
    scope_tags=["alert", "incident", "latency", "trace", "mitigation", "db"],
)

mem.remember("Alert PAGE-99 fired: payments p99 latency at 2.3s")   # gated save
# conversational input? pass who said it and when — every extracted fragment
# keeps the anchors, and relative dates resolve to absolute ("last Friday"
# becomes "[= Friday, 5 May 2023]") with calendar math, no LLM:
mem.remember("The pool was resized last Friday. We watch it daily now.",
             speaker="Raj", when="8 May, 2023")
hits = mem.search("what is going on with payments?",
                  agent="oncall_sre", task_tags=["incident", "latency"])
for h in hits:
    print(h["value"], h["promotion"]["lane"])   # every result explains WHY it was served
```

No profile registered? `search()` is plain hybrid retrieval (keyword + vector + RRF), so you can adopt gradually.

## Plug into a Hermes agent

Three steps on any machine with Hermes installed:

```bash
# 1. put the engine in Hermes's environment
pip install engram-lite

# 2. install the memory plugin
hermes plugins install engrammemory-labs/engram-lite/hermes-plugin/engram

# 3. activate and configure it
hermes memory setup     # choose "engram", answer one question — what is this
                        # agent? (e.g. "DevOps engineer") — everything else is
                        # derived; leave it empty for plain memory
```

(Working from a clone or a zip instead? One command does both steps: `./install-hermes.sh`)

Then just `hermes chat`. From that point: every turn is auto-captured through the salience gate, the agent boots with a snapshot of what it already knows, and `memory_search` / `memory_write` / `memory_diagnose` are available as in-loop tools. Restart the agent; it remembers. Point several agents at one `db_path` (a wizard field) and the lane model keeps each one's serving scoped.

Wondering whether memory is actually operating? `engram doctor` gives the one-screen answer (configs, stores, fact counts, why turns were kept or skipped), and `engram diagnose` narrates the capture gate's decisions in plain English. An empty store early on is normal: questions and chatter are deliberately not stored. And if `hermes plugins list` ever shows the plugin as disabled, `hermes plugins enable engram` brings the listing in line — memory keeps working either way, because Hermes loads the provider from its memory config, not from that flag.

Building your own harness instead of Hermes? The same provider is a plain Python class:

```python
from engram.integrations.hermes import EngramMemoryProvider

memory = EngramMemoryProvider(
    db_path="~/.engram/memory.db",
    agent="oncall_sre", persona="on-call SRE", domain="sre-devops",
    scope_tags=["alert", "incident", "latency", "trace", "mitigation"],
)
# call initialize / system_prompt_block / prefetch / sync_turn / shutdown
```

## Plug into Claude Code

```bash
pip install engram-lite
engram claude setup        # one question, one consent — memory is live
```

Setup wires three hooks into Claude Code (with your consent, a backup, and
`engram claude uninstall` to reverse it): every prompt is captured through the
salience gate, memories that fit the current prompt are served each turn, and
a memory snapshot is re-injected after `/clear` and compaction — the moments
context is lost are the moments memory matters. Everything fails open: if the
local daemon is down, the session simply behaves like stock Claude Code.

Prefer a plugin-native install? This repo is a Claude Code plugin marketplace:
`/plugin marketplace add engrammemory-labs/engram-lite`, then
`/plugin install engram@engram`. Details in [`claude-plugin/`](claude-plugin/README.md).

## Plug into OpenClaw

A memory-slot plugin lives in [`openclaw-plugin/`](openclaw-plugin/README.md):
auto-recall before each prompt, auto-capture after each run,
`memory_search` / `memory_store` / `memory_diagnose` tools, and a one-question
`openclaw engram setup`. It supervises the same local daemon the Claude Code
hooks use — point both at one store and your agents share memory, each served
its own lane. A NemoClaw sandbox recipe ships alongside it; the memory loop
runs with zero egress, proven by a harness you can re-run
([`openclaw-plugin/nemoclaw/proof/`](openclaw-plugin/nemoclaw/proof)).

## The numbers behind the claims

### LoCoMo: long-conversation memory, zero LLM in the memory layer

LLM-judge score (J) on LoCoMo categories 1–4 (1,540 questions), under the evaluation protocol standard in the published literature:

| System | Overall J | Single-hop | Multi-hop | Temporal | Open-domain |
|---|---|---|---|---|---|
| Full-context baseline (no memory system) | 72.9 | — | — | — | — |
| **engram-lite** | **68.3** | **74.8** | **55.3** | **68.5** | 49.0 |

- **Zero LLM calls to build the memory.** Ingesting the 5,882-turn corpus costs $0 and takes about 2 minutes, fully local.
- On the adversarial category (trick questions where the right answer is "no information"): J 64.6. Honest abstention is a feature, not a failure mode.
- Retrieval alone (no LLM anywhere): 85.7% of questions have their evidence served in the top-30, vs 59.9% for grep over the raw transcript. Two from-scratch runs produce the identical digest.
- Answerer and judge: claude-haiku-4-5 (measured judge sensitivity: ±2 for prompt wording, ±0 for judge model). LoCoMo: Maharana et al. ([arXiv:2402.17753](https://arxiv.org/abs/2402.17753)). Full protocol notes in [`benchmarks/locomo/`](https://github.com/engrammemory-labs/engram-lite/blob/main/benchmarks/locomo/README.md).

### What memory costs your prompt, per turn

Measured on a real 15-agent shared store (92 memories, 105 probes), same recall quality question for all three strategies:

| Strategy | Tokens injected/turn | Cross-domain leaks | Out-of-lane abstention |
|---|---|---|---|
| Whole memory file in the prompt | 2,071 | 85 per turn | 0% |
| Keyword top-10 over the file | 163 | 5.7 per turn | 0% |
| **engram-lite conditioned serving** | **29** | **0** | **100%** |

And the memory layer itself bills nothing: zero LLM calls at capture, zero at serving. (LLM-extraction pipelines spend ~2 model calls per captured turn to build memory; engram-lite spends none, and nothing leaves your machine.)

### Lane serving at scale

One shared store, four profiled lanes, real embedder, measured through the
daemon's HTTP path on an Apple-silicon laptop (`benchmarks/lane_stress.py`,
re-runnable):

| Store size | Search p50 | p95 | Top-1 retrieval | Cross-lane leaks |
|---|---|---|---|---|
| 5,000 facts | 35 ms | 48 ms | 98.5% | 0/100 |
| 20,000 facts | 94 ms | 114 ms | 99.0% | 0/100 |

### CAMP-Bench: conditioned serving vs the flat pile

We built a benchmark (CAMP-Bench) with human-authored gold across two engineering domains, 30 cases each: which memory should each role be served, per situation. (An internal benchmark: treat these as our numbers, not independently checkable ones. The LoCoMo results above are the externally replayable ones.) **This library, measured on its real read path** (three-signal retrieval funnel + lane promotion, offline stub embedder, k=3):

| domain | precision | recall | profile discrimination | correct abstention |
|---|---|---|---|---|
| sre-devops | 0.63 | 0.87 | 0.91 | 6/6 |
| backend-engineering | 0.56 | 0.85 | 0.92 | 3/3 |
| *flat serving (any domain)* | *~0.23* | *~0.55* | *0.00* | *0* |

Downstream, with a live agent and an independent judge on the same data, the lane model took task success from **40% (flat) to 80%** and cut hallucination from **33% to 10%**, while an agent with no memory scored 17%. Flat memory hallucinated more than no memory at all: **wrong memory is worse than no memory.**

## How serving works (the lane model)

Each fact carries tags (extracted at write time from your profiles' vocabulary, or passed explicitly). At recall time:

```
lane = task_tags ∩ agent.scope_tags          # what this situation is about
                                             # that THIS agent owns
empty lane  -> serve nothing (honest abstention)
otherwise   -> score facts by IDF-weighted overlap with the lane,
               gate out other domains, apply a relevance floor,
               serve at most k (fewer when little truly fits)
```

Deterministic, microseconds, no LLM in the read path, and every served fact carries `why` (the lane tags it hit and its score).

## Development

```bash
pip install -e ".[dev]"
ruff check src
```

## License
Apache-2.0.
