# Contributing to engram-lite

Thanks for helping build better agent memory.

## Dev setup

```bash
git clone https://github.com/novarque/engram-lite.git
cd engram-lite
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ENGRAM_EMBEDDER=hash pytest -q     # offline, no model download
ruff check src tests
```

## Ground rules

- Tests accompany every change. The suite must stay offline-runnable
  (`ENGRAM_EMBEDDER=hash`), fast, and green on 3.10 through 3.12.
- The engine stays deterministic and local: no LLM calls, no network calls, no
  telemetry. Anything that needs a model or a server is out of scope for
  engram-lite.
- `storage/repository.py` is the only module that writes SQL.
- Never print to stdout in library or MCP code paths (stdout is the MCP stdio
  protocol channel); use stderr.
- Keep the public API small. If a knob can be a constant in `config.py`, it is
  not a parameter.

## Reporting issues

A failing test or a copy-pasteable repro script beats a description. Memory DBs
can contain personal data, so please do not attach real `.db` files.
