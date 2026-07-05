# LoCoMo benchmark — results

engram-lite's LoCoMo results. Every run is deterministic on the memory
side: two from-scratch runs of the same protocol produce identical outputs.

## Results (2026-07-04, this harness)

LLM-judge score (J), mem0's published protocol (categories 1–4, higher = better):

| System | Overall J | Single-hop | Multi-hop | Temporal | Open-domain |
|---|---|---|---|---|---|
| Full-context (no memory system) | 72.9 | — | — | — | — |
| Mem0-graph | 68.4 | 65.7 | 47.2 | 58.1 | 75.7 |
| **engram-lite (zero LLM in memory)** | **68.3** | **74.8** | **55.3** | **68.5** | 49.0 |
| Mem0 | 66.9 | 67.1 | 51.2 | 55.5 | 72.9 |
| Zep* | 66.0 | 61.7 | 41.4 | 49.3 | 76.6 |
| LangMem | 58.1 | 62.2 | 47.9 | 23.4 | 71.1 |
| OpenAI memory | 52.9 | 63.8 | 42.9 | 21.7 | 62.3 |

Rows for the other systems and the full-context baseline: mem0's ECAI-2025 paper ([arXiv:2504.19413](https://arxiv.org/abs/2504.19413)),
gpt-4o-mini answerer + judge. LoCoMo benchmark: Maharana et al.
([arXiv:2402.17753](https://arxiv.org/abs/2402.17753)), CC BY-NC 4.0.
\* Zep disputes mem0's evaluation of their system and published a higher
self-reported number ([the dispute](https://github.com/getzep/zep-papers/issues/5));
the row shown is mem0's published re-run, consistent with the rest of the table.

engram-lite row: this harness, claude-haiku-4-5 answerer + judge
(disclosed difference; judge sensitivity measured at ±2 for wording, ±0 for
judge model). Adversarial category (446 trick questions, correct = abstain,
excluded by mem0's protocol): engram-lite J 64.6 (bge-base engine; 64.3 on bge-small).

LLM calls to build the memory from 5,882 conversation turns: engram-lite 0;
mem0 approximately 2 per turn, by design of its `add()` pipeline
([arXiv:2504.19413](https://arxiv.org/abs/2504.19413)). engram-lite ingestion
cost: $0, ~2 minutes, fully local.

## Protocol decisions (all in-code, all disclosed)

- One fresh store per conversation; the real product write path
  (`Memory.remember(text, speaker=..., when=...)`) — salience gate,
  extraction, consolidation, relative-date resolution all run as shipped.
- Recency weighting off for replays (months of conversation compress into
  minutes of ingestion; wall-clock recency would be meaningless and is the
  only nondeterminism source).
- `blip_caption` image captions ingested (mem0 ingests them too).
- Track B parity prompts mirror the semantics of mem0's published harness:
  no abstention option, generous same-topic/same-period judging, 60-memory
  retrieval budget (their two per-speaker searches × 30).
- Category 5 excluded from the headline J (their aggregation), reported
  separately as the abstention receipt.
- The answer prompt includes three worked examples of relative-date
  arithmetic ("last Friday" + a dated memory → the absolute date). Disclosed
  because temporal is a headline win: the examples teach the FORMAT; the
  dates themselves are resolved deterministically at capture by the engine.
- Answering/judge model is a disclosed difference (haiku vs gpt-4o-mini).
  mem0 has since published a higher figure (92.5%) from a different harness
  (gpt-5 judge, top-200 retrieval, partial credit); the protocols differ
  enough that it is not directly comparable to their paper's 66.9 or to the
  numbers here.
