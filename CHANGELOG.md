# Changelog

## 0.2.0 — one engine, every tool

- **`engram serve` — the local memory daemon.** One loopback-only process per
  store serves every non-Python integration over localhost HTTP: bearer-token
  auth on by default, bodies capped, structured JSON logs, clean shutdown with
  WAL checkpoint. Built entirely on the standard library — zero new
  dependencies. `--ensure` makes it start-or-reuse: the daemon persists its
  connection contract next to the store (`<db>.serve.json`, 0600) and holds a
  kernel lock for its lifetime, so exactly one daemon ever runs per store —
  safe under concurrent callers, crashes, and restarts.
- **Claude Code integration.** `pip install engram-lite && engram claude setup`
  — one question, one consent — wires memory into Claude Code via hooks: every
  prompt captured, role-scoped memories served each turn, and memory that
  survives `/clear` and compaction. `engram claude uninstall` reverses it
  cleanly. Also installable as a plugin from this repo's marketplace.
- **OpenClaw integration.** A memory-slot plugin (`openclaw-plugin/`):
  auto-recall before each prompt, auto-capture after each run,
  `memory_search` / `memory_store` / `memory_diagnose` tools, and a
  one-question `openclaw engram setup`. The plugin supervises the daemon and
  never downloads anything unless explicitly allowed (`allowDownload`, off by
  default, version-pinned). A NemoClaw sandbox recipe ships alongside it —
  the memory loop passes a 10-check proof with the network physically removed.
- **The eviction cap is a property of the store** (persisted in the db,
  `ENGRAM_MAX_FACTS` to raise it), so a store shared by several integrations
  keeps its ceiling regardless of which process opens it.
- **First-run observability** (shaped by early-tester feedback): `engram
  doctor` answers "installed, wired, operating?" in one screen — configs,
  stores, fact counts, ledger summary, and the next command to run; `engram
  diagnose` shows the decision ledger in plain English (why each turn was
  kept or skipped); `engram status` explains an empty store ("engine healthy,
  captures skipped as non-durable") instead of a bare zero, and points at
  integration stores when the default path is empty. All read-only, safe
  against a running daemon.
- Store files (db, WAL) are created private (0600); daemon logs rotate at 5 MB.
- Lane serving measured at scale (Apple-silicon laptop, real embedder):
  20,000 facts across 4 lanes in one shared store — search p95 114 ms, top-1
  retrieval 99%, zero cross-lane leaks in 200 adversarial probes.

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
