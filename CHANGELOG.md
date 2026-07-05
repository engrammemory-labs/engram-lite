# Changelog

## 0.1.0 — first public release

- Local memory engine: SQLite + FTS5 + sqlite-vec in one file; salience gate,
  ADD / UPDATE / REINFORCE consolidation, versioned facts with provenance,
  expiry, and bounded size with eviction.
- **Conditioned promotion (the lane model):** register a profile per agent
  (persona / domain / scope_tags) and every recall is served for that agent's
  lane — IDF-weighted, relevance-floored, with honest abstention and an
  explainable `why` on every served fact. Measured on our internal CAMP-Bench:
  profile discrimination 0.00 → 0.90, task success 40% → 80%, hallucination
  cut to a third versus a flat shared pile.
- Hermes integration: `engram.integrations.hermes.EngramMemoryProvider`
  (initialize / system_prompt_block / prefetch / sync_turn / tools / shutdown).
