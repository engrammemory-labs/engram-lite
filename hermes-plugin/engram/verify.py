"""Verify the engram Hermes plugin against the REAL hermes-agent ABC.

Drives the provider exactly the way hermes's MemoryManager does (same call
shapes, same kwargs, same expectations), including a process-restart pickup.

    PYTHONPATH=$HERMES_AGENT ENGRAM_EMBEDDER=hash python verify.py

Needs: the hermes-agent source on PYTHONPATH + engram-lite installed. No LLM,
no network, no key.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ENGRAM_EMBEDDER", "hash")

from agent.memory_provider import MemoryProvider  # the REAL ABC  # noqa: E402
from engram import __name__ as _engram_ok         # engram-lite importable  # noqa: E402
import importlib.util                              # noqa: E402

spec = importlib.util.spec_from_file_location(
    "engram_plugin", str(Path(__file__).parent / "__init__.py"))
plugin_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_mod)
Provider = plugin_mod.EngramHermesProvider

PASS = []


def check(label, cond):
    PASS.append((label, bool(cond)))
    print(f"  {'✓' if cond else '✗ FAIL'}  {label}")
    return cond


def main() -> int:


    home = tempfile.mkdtemp(prefix="hermes-home-")
    print(f"── verify against real ABC (hermes_home={home}) ──")

    p = Provider()
    check("subclasses the real MemoryProvider ABC", isinstance(p, MemoryProvider))
    check("name property", p.name == "engram")
    check("is_available() without initialize/network", p.is_available() is True)

    # config via the setup flow
    p.save_config({"persona": "on-call SRE", "domain": "sre-devops",
                   "scope_tags": "alert,incident,latency,trace,mitigation,db",
                   "agent": "oncall_sre"}, home)
    check("save_config wrote config.json",
          (Path(home) / "engram" / "config.json").exists())

    # lifecycle, called EXACTLY like MemoryManager does
    p.initialize(session_id="sess-1", hermes_home=home, platform="cli",
                 agent_context="primary", agent_identity="coder")

    schemas = p.get_tool_schemas()
    check("tool schemas are FLAT OpenAI shape (name/description/parameters)",
          all(set(s) >= {"name", "description", "parameters"} and
              "input_schema" not in s for s in schemas))

    out = p.handle_tool_call("memory_write",
                             {"text": "Alert PAGE-99 fired: payments p99 latency 2.3s",
                              "tags": ["alert", "latency", "incident"]})
    check("handle_tool_call returns a JSON string", isinstance(json.loads(out), dict))

    p.sync_turn("Trace tr-77 shows db pool saturation; we bumped db_pool to 40",
                "Noted the mitigation.", session_id="sess-1", messages=[])
    check("sync_turn accepts manager call shape (positional + kwargs)", True)

    got = p.prefetch("what happened with payments latency?", session_id="sess-1")
    check("prefetch(session_id=...) serves lane memory", "PAGE-99" in got or "db" in got.lower())

    block = p.system_prompt_block()
    check("system_prompt_block carries the lane snapshot", "Memory (engram)" in block)

    p.on_memory_write("add", "memory", "Rollback runbook: argocd app rollback payments-api",
                      {"write_origin": "builtin"})
    p.on_pre_compress([{"role": "user", "content": "The canary policy is auto-rollback at 2% errors"},
                       {"role": "assistant", "content": [{"type": "text",
                        "text": "Confirmed: canary auto-rollback threshold is 2 percent."}]}])
    got = p.prefetch("what is the canary policy?", session_id="sess-1")
    check("on_pre_compress captured facts are recallable", "2" in got and "canary" in got.lower())
    p.shutdown()

    # restart pickup — a brand-new provider instance, same home
    p2 = Provider()
    p2.initialize(session_id="sess-2", hermes_home=home, platform="cli",
                  agent_context="primary", agent_identity="coder")
    check("restart pickup: fresh instance boots with prior memory",
          "Memory (engram)" in p2.system_prompt_block())

    # non-primary contexts must not write
    p3 = Provider()
    p3.initialize(session_id="cron-1", hermes_home=home, platform="cron",
                  agent_context="cron", agent_identity="coder")
    res = json.loads(p3.handle_tool_call("memory_write", {"text": "cron noise should not land"}))
    check("cron/subagent context: writes are refused", "skipped" in res)
    p3.sync_turn("cron chatter", "cron reply", session_id="cron-1")
    got = p2.prefetch("cron noise", session_id="sess-2")
    check("cron content never entered the store", "cron noise" not in got)
    p2.shutdown(); p3.shutdown()

    failed = [label for label, ok in PASS if not ok]
    print(f"\n{'ALL ' + str(len(PASS)) + ' CHECKS PASSED ✓' if not failed else 'FAILURES: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
