# engram-lite

**Local memory for AI agents, served by persona, domain, and task. Survives restarts. One machine, many agents.**

Most agent memory is a shared pile: every agent gets the same top-k for the same query. In our benchmark (methodology below), an agent fed flat shared memory **hallucinated more than an agent given no memory at all.** Serving each agent the memory that fits **who it is** (its persona, its domain, the task in front of it) **doubled task success (40% to 80%)** and **cut hallucination to a third.**

And the memory layer never calls an LLM. On **LoCoMo** — the long-conversation memory benchmark — engram-lite scores **J 68.3** with **zero LLM calls and $0 to build the memory**. The protocol, footnotes, a harness you can re-run yourself, and the full comparison against the leading LLM-based memory systems live in [`benchmarks/locomo/`].

engram-lite is that serving layer, small enough to run on your laptop:

- **Conditioned serving.** Register a profile per agent (persona / domain / scope). The same question serves your on-call SRE agent the alert, the trace, and the mitigation runbook, and serves your release agent the deploy, the rollback, and the freeze policy. Out-of-lane questions correctly serve nothing.
- **Restart-proof.** One SQLite file holds everything. Your agent restarts tomorrow and picks up exactly what it knew.
- **Self-cleaning.** A salience gate skips junk (code, command output, questions). New facts are de-duplicated, updates supersede old versions, stale facts expire, and the store stays bounded.
- **Zero infrastructure.** No server, no cloud, no accounts, no telemetry. SQLite plus a local embedding model. `pip install` and you are running.
- **Explainable forgetting.** Every drop, merge, truncation, and abstention is recorded with the rule that fired (`Memory.decisions()`). "Why don't you remember X?" has an answer — something an LLM-written memory can't give you, because its keep/drop decisions live inside model weights.
- **Deterministic and replayable.** Same transcript in, same memory out — byte-identical across rebuilds. Memory failures can be diffed, bisected, and regression-gated like any other bug.
- **Plugs into what you build with.** A Hermes memory provider, or three lines of Python.

> engram-lite is single-machine by design. A team/hosted edition, **engram-core**, is in development — reach out if that is what you need.

## Install

```bash
pip install engram-lite
```

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
#    (before the PyPI release, install straight from the repo instead:
#     pip install git+https://github.com/engrammemory-labs/engram-lite.git)

# 2. install the memory plugin
hermes plugins install engrammemory-labs/engram-lite/hermes-plugin/engram

# 3. activate and configure it
hermes memory setup     # choose "engram"; the wizard asks persona / domain /
                        # scope_tags — fill all three for conditioned serving,
                        # or leave them empty for plain memory
```

Then just `hermes chat`. From that point: every turn is auto-captured through the salience gate, the agent boots with a snapshot of what it already knows, and `memory_search` / `memory_write` / `memory_diagnose` are available as in-loop tools. Restart the agent; it remembers. Point several agents at one `db_path` (a wizard field) and the lane model keeps each one's serving scoped.

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

## The numbers behind the claims

### LoCoMo: long-conversation memory, zero LLM in the memory layer

LLM-judge score (J) on LoCoMo categories 1–4 (1,540 questions), under the evaluation protocol standard in the published literature:

| System | Overall J | Single-hop | Multi-hop | Temporal | Open-domain |
|---|---|---|---|---|---|
| Full-context baseline (no memory system) | 72.9 | — | — | — | — |
| **engram-lite** | **68.3** | **74.8** | **55.3** | **68.5** | 49.0 |

- **Zero LLM calls to build the memory.** Ingesting the 5,882-turn corpus costs $0 and takes about 2 minutes, fully local.
- On the adversarial category (trick questions where the right answer is "no information"): J 64.6. Honest abstention is a feature, not a failure mode.
- Retrieval alone (no LLM anywhere, free to re-run): 85.7% of questions have their evidence served in the top-30, vs 59.9% for grep over the raw transcript. Two from-scratch runs produce the identical digest.
- Answerer and judge: claude-haiku-4-5 (measured judge sensitivity: ±2 for prompt wording, ±0 for judge model). LoCoMo: Maharana et al. ([arXiv:2402.17753](https://arxiv.org/abs/2402.17753)). Everything is replayable from [`benchmarks/locomo/`].

### What memory costs your prompt, per turn

Measured on a real 15-agent shared store (92 memories, 105 probes), same recall quality question for all three strategies:

| Strategy | Tokens injected/turn | Cross-domain leaks | Out-of-lane abstention |
|---|---|---|---|
| Whole memory file in the prompt | 2,071 | 85 per turn | 0% |
| Keyword top-10 over the file | 163 | 5.7 per turn | 0% |
| **engram-lite conditioned serving** | **29** | **0** | **100%** |

And the memory layer itself bills nothing: zero LLM calls at capture, zero at serving. (LLM-extraction pipelines spend ~2 model calls per captured turn to build memory; engram-lite spends none, and nothing leaves your machine.)

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
ENGRAM_EMBEDDER=hash pytest -q     # offline, fast
ruff check src tests
```

## License
Apache-2.0.
