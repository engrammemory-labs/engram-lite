"""The `engram` command — dispatches to subcommands.

  engram demo      interactive REPL
  engram status    print current env settings
  engram rebuild   re-embed a store after changing the embedding model
  engram serve     local memory daemon (loopback HTTP) for non-Python hosts
"""
from __future__ import annotations

import argparse
import os

from . import demo


def _run_selftest(args: argparse.Namespace) -> int:
    """Verify the engine works end-to-end (offline, no model download)."""
    import os
    import tempfile

    from ..core.memory import Memory
    from ..embeddings import HashEmbedder

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        m = Memory(path, embedder=HashEmbedder())
        m.remember("selftest: the payments service is owned by Bob", subject="selftest")
        hits = m.search("who owns payments?", subject="selftest")
        m.close()
        if hits:
            print("✓ engine OK — sqlite-vec + FTS5 + vector search working")
            return 0
        print("✗ engine ran but recall failed")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"✗ engine FAILED: {exc}")
        return 1
    finally:
        os.unlink(path)


def _run_serve(args) -> int:
    import json

    if args.ensure:
        from ..ensure import ensure
        try:
            state = ensure(args.db, agent=args.agent)
        except TimeoutError as exc:
            print(f"engram serve --ensure: {exc}", file=__import__("sys").stderr)
            return 1
        # same contract shape as a fresh daemon prints, so callers need
        # exactly one parser for both paths
        print(json.dumps({"engram_serve": state}), flush=True)
        return 0

    from ..server import serve
    serve(db_path=args.db, port=args.port,
          token=(None if args.no_token else "auto"),
          agent=args.agent,
          state_file=args.state)
    return 0


def _run_rebuild(args) -> int:
    from ..core.memory import Memory
    res = Memory.reembed(args.db)
    print(f"re-embedded {res['reembedded']} facts at {res['dim']}-dim "
          f"({res['model']}) — store ready")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    import os
    import sqlite3

    from .. import config
    from ..settings import Settings

    s = Settings.from_env()
    if getattr(args, "db", None):
        import dataclasses
        s = dataclasses.replace(s, db_path=os.path.expanduser(args.db))
    print(s.summary())
    # show "how full" without loading the embedding model — a plain sqlite read
    if os.path.exists(s.db_path):
        con = sqlite3.connect(s.db_path)
        try:
            cur = con.execute(
                "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL "
                "AND validation_status = 'fresh'"
            ).fetchone()[0]
            tot = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            pages = con.execute("PRAGMA page_count").fetchone()[0]
            psize = con.execute("PRAGMA page_size").fetchone()[0]
            print(f"usage: {cur} current / {tot} total facts · cap {config.MAX_FACTS} "
                  f"· db {pages * psize / 1024:.0f} KB")
            if cur == 0:
                from . import stores as _stores
                st = _stores.store_stats(s.db_path)
                skips = (st or {}).get("decisions_total", 0)
                if skips:
                    print(f"engine healthy — no facts stored yet; the capture gate has "
                          f"judged {skips} event(s) so far (`engram diagnose` shows why)")
                else:
                    print("engine healthy — no conversation has reached this store yet")
        except sqlite3.Error:
            print("usage: (no memory yet)")
        finally:
            con.close()
    else:
        print("usage: (no DB at this path yet)")
        from . import stores as _stores
        others = [(c, _stores.store_stats(c["path"])) for c in _stores.discover_stores()
                  if c["path"] != s.db_path]
        found = [(c, st) for c, st in others if st]
        if found:
            print("stores that DO exist on this machine:")
            for c, st in found:
                print(f"  {c['label']}: {c['path']} ({st['current']} facts) "
                      f"— inspect with --db")
        else:
            print("pass --db <path> to inspect a specific store")
    return 0


def _run_claude_setup(args) -> int:
    """One question, then the Claude Code adapter is configured end to end."""
    import json
    import os

    from ..ensure import ensure
    from .. import profiles

    persona = ""
    for _ in range(3):
        persona = input('What is this agent? (one sentence, e.g. "software engineer"): ').strip()
        if len(persona) >= 2:
            break
    if len(persona) < 2:
        print("setup aborted: a persona is required — it is the only question.")
        return 1

    config_path = os.path.expanduser(
        os.environ.get("ENGRAM_CLAUDE_CONFIG", "~/.engram/claude.json"))
    db = os.path.expanduser(args.db)
    agent = args.agent

    print("Checking the memory engine…")
    try:
        state = ensure(db, agent=agent)
    except TimeoutError as exc:
        print(exc)
        print("Install the engine, then re-run `engram claude setup`:")
        print("  pip install engram-lite   (or uv tool install / pipx install)")
        return 1

    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}/profile",
        data=json.dumps({"persona": persona, "agent": agent}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {state['token']}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        json.loads(res.read())
    domain, scope = profiles.derive_profile(persona)
    print(f'Engine OK — profile registered: agent "{agent}" → domain "{domain}" '
          f"(scope: {', '.join(scope[:6])}…)")

    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    # record the exact engram entry point THIS setup ran under, so the hooks
    # work even when `engram` is not on the interactive PATH (venv installs)
    import sys as _sys
    sibling = os.path.join(os.path.dirname(_sys.executable), "engram")
    engram_cmd = sibling if os.path.exists(sibling) else "engram"
    payload = {"persona": persona, "agent": agent, "db": db,
               "engramCmd": engram_cmd}
    fd = os.open(config_path + ".tmp", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(config_path + ".tmp", config_path)
    print(f"Adapter config written: {config_path}")

    from ..integrations.claude import settings as claude_settings
    target = claude_settings.settings_path()
    try:
        answer = input(f"Wire the hooks into {target}? [Y/n]: ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("", "y", "yes"):
        try:
            result = claude_settings.wire(target, engram_cmd)
        except ValueError as exc:
            print(f"Could not edit {target} ({exc}); nothing was changed.")
            print("Add the hooks manually, or run Claude Code with:")
            print("  claude --plugin-dir <repo>/claude-plugin")
            return 2
        print(f"Hooks wired into {result['path']}"
              + (f" (backup: {result['backup']})" if result["backup"] else "") + ".")
        print()
        print("Done. Restart Claude Code — memory is live from the next session.")
        print("Undo anytime with: engram claude uninstall")
    else:
        print("Skipped. Alternatives: run `claude --plugin-dir <repo>/claude-plugin`,")
        print("or re-run `engram claude setup` when ready.")
    return 0


def _run_diagnose(args) -> int:
    """The decision ledger, human-first: what was kept, skipped, merged —
    and the rule that fired, in plain English."""
    from . import stores

    path = os.path.expanduser(args.db) if args.db else None
    if path is None:
        existing = [(c, stores.store_stats(c["path"])) for c in stores.discover_stores()]
        existing = [(c, st) for c, st in existing if st]
        if not existing:
            print("no store found — pass --db <path>")
            return 1
        c, _ = existing[0]
        path = c["path"]
        print(f"store: {path} ({c['label']})")

    rows = stores.recent_decisions(path, limit=args.limit, kind=args.kind)
    if not rows:
        print("ledger is empty: no capture or serve decision has been made on "
              "this store yet — memory has not seen any conversation here.")
        return 0
    for r in rows:
        snippet = " ".join((r["snippet"] or "").split())[:60]
        print(f"{r['ts']}  {r['kind']:<14} {r['rule']}")
        if snippet:
            print(f"{'':21}“{snippet}”")
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    print()
    print("in plain terms:")
    seen_rules = []
    for r in rows:
        if r["rule"] not in seen_rules:
            seen_rules.append(r["rule"])
    for rule in seen_rules[:4]:
        print(f"  · {stores.explain_rule(rule)}")
    if all(r["kind"].startswith("capture") or r["kind"].endswith("skip")
           for r in rows):
        print("  · nothing here is an error: the gate stores durable facts and "
              "skips questions, chatter, and machine output")
    return 0


def _run_doctor(args) -> int:
    """One command that answers: is memory installed, wired, and operating —
    and if not, what is the next command to run."""
    import importlib.metadata

    from . import stores

    def mark(ok):  # noqa: ANN001
        return "✓" if ok else "✗"

    try:
        version = importlib.metadata.version("engram-lite")
    except importlib.metadata.PackageNotFoundError:
        version = "not installed"
    print(f"{mark(version != 'not installed')} engram-lite {version}")

    hermes_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    plugin_dir = os.path.join(hermes_home, "plugins", "engram")
    hermes_present = os.path.isdir(hermes_home)
    if hermes_present:
        print(f"  HERMES_HOME: {hermes_home} (Hermes keeps its own store under "
              f"{os.path.join(hermes_home, 'engram')} — per-profile isolation, "
              "deliberately not the standalone default)")
        print(f"{mark(os.path.isdir(plugin_dir))} Hermes plugin at {plugin_dir}")
        cfg_yaml = os.path.join(hermes_home, "config.yaml")
        provider_active = False
        try:
            with open(cfg_yaml, "r", encoding="utf-8") as f:
                provider_active = "provider: engram" in f.read()
        except OSError:
            pass
        print(f"{mark(provider_active)} Hermes memory provider set to engram "
              f"({cfg_yaml})")
        enabled = None
        try:
            import yaml  # available inside a Hermes venv; optional elsewhere
            plug = (yaml.safe_load(open(cfg_yaml)) or {}).get("plugins", {})
            enabled = "engram" in (plug.get("enabled") or [])
        except Exception:  # noqa: BLE001
            pass  # no yaml parser here: skip the advisory check
        if enabled is False:
            print("  note: plugin not in the enabled list — memory works anyway "
                  "(the provider loads via memory.provider), but `hermes plugins "
                  "enable engram` makes `hermes plugins list` agree with reality")
        if provider_active:
            print("  provider tools when active: memory_search, memory_write, "
                  "memory_diagnose (surfaced by Hermes inside chat; a CLI "
                  "'Unknown tool' outside chat is expected)")

    claude_cfg = os.path.expanduser(
        os.environ.get("ENGRAM_CLAUDE_CONFIG", "~/.engram/claude.json"))
    if os.path.exists(claude_cfg):
        print(f"✓ Claude Code adapter config at {claude_cfg}")

    standalone = os.path.expanduser("~/.engram/memory.db")
    if not os.path.exists(standalone):
        print(f"  standalone default {standalone}: absent (only integrations "
              "with their own paths are in use)")

    print()
    any_store = False
    empty_with_skips = None
    for c in stores.discover_stores():
        st = stores.store_stats(c["path"])
        if not st:
            continue
        any_store = True
        daemon = stores.daemon_state(c["path"])
        writable = os.access(os.path.dirname(c["path"]) or ".", os.W_OK)
        profiles = ", ".join(p["agent"] for p in st["profiles"]) or "(none)"
        print(f"store [{c['label']}] {c['path']}")
        print(f"  {mark(writable)} writable · facts {st['current']} current / "
              f"{st['total']} total"
              + (f" · cap {st['cap']}" if st["cap"] else "")
              + (f" · daemon pid {daemon['pid']}" if daemon else ""))
        print(f"  profiles: {profiles}")
        if st["decisions_total"]:
            kinds = ", ".join(f"{k}×{v}" for k, v in st["decision_counts"].items())
            print(f"  recent ledger: {kinds}")
        if st["current"] == 0 and st["decisions_total"] > 0:
            empty_with_skips = c["path"]
        print()

    if not any_store:
        print("no store exists yet")
        print("next: `engram claude setup` (Claude Code) or `hermes memory setup` (Hermes)")
    elif empty_with_skips:
        print("verdict: engine healthy and wired — the store is empty because the")
        print("capture gate skipped what it saw (questions/chatter are not stored).")
        print(f"next: state a durable fact in a conversation, then "
              f"`engram diagnose --db {empty_with_skips}`")
    else:
        print("verdict: memory is operating")
        print("next: `engram diagnose` to watch keep/skip/serve decisions")
    return 0


def _run_claude_hook(args) -> int:
    """Dispatch a Claude Code hook event to the packaged adapter. The
    fail-open guarantee is enforced HERE too: no exception may reach the
    user's session as a traceback or nonzero exit."""
    try:
        from ..integrations.claude import session_start, stop, user_prompt
        module = {"session-start": session_start,
                  "user-prompt": user_prompt,
                  "stop": stop}[args.event]
        return int(module.main() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _run_claude_uninstall(args) -> int:
    from ..integrations.claude import settings as claude_settings

    result = claude_settings.unwire(claude_settings.settings_path())
    if result.get("error"):
        print(result["error"])
        return 1
    print(f"Removed {result['removed']} engram hook entr"
          f"{'y' if result['removed'] == 1 else 'ies'} from {result['path']}.")
    print("Left in place (delete manually if you want them gone):")
    print("  ~/.engram/claude.json (adapter config) and the memory store itself.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram", description="engram — local agentic memory")
    sub = parser.add_subparsers(dest="command")

    demo.add_parser(sub)
    st = sub.add_parser("status", help="print the current env settings")
    st.add_argument("--db", default=None, help="inspect a specific store (default: ENGRAM_DB_PATH)")
    st.set_defaults(func=_run_status)
    dg = sub.add_parser("diagnose", help="the decision ledger in plain English")
    dg.add_argument("--db", default=None, help="store to inspect (default: first discovered)")
    dg.add_argument("--limit", type=int, default=10, help="entries to show (default 10)")
    dg.add_argument("--kind", default=None, help="filter, e.g. capture-skip, serve-abstain")
    dg.set_defaults(func=_run_diagnose)
    dr = sub.add_parser("doctor", help="installed, wired, operating? one answer")
    dr.set_defaults(func=_run_doctor)
    sub.add_parser("selftest", help="verify the engine works (offline)").set_defaults(func=_run_selftest)
    rb = sub.add_parser("rebuild", help="re-embed a store after changing ENGRAM_EMBEDDER_MODEL")
    rb.add_argument("db", help="path to the memory .db file")
    rb.set_defaults(func=_run_rebuild)
    sv = sub.add_parser("serve", help="local memory daemon (binds 127.0.0.1 only)")
    sv.add_argument("--db", default="~/.engram/memory.db", help="store path")
    sv.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    sv.add_argument("--agent", default="default", help="default agent identity")
    sv.add_argument("--no-token", action="store_true",
                    help="disable bearer auth (local experimentation only)")
    sv.add_argument("--state", default=None,
                    help="persist the startup contract to this file (0600); "
                         "removed on clean shutdown")
    sv.add_argument("--ensure", action="store_true",
                    help="idempotent: reuse a live daemon for --db or spawn one "
                         "detached, then print its contract and exit")
    sv.set_defaults(func=_run_serve)

    cl = sub.add_parser("claude", help="Claude Code adapter commands")
    cl_sub = cl.add_subparsers(dest="claude_command")
    cs = cl_sub.add_parser("setup", help="one-question setup for the Claude Code plugin")
    cs.add_argument("--db", default="~/.engram/memory.db", help="shared store path")
    cs.add_argument("--agent", default="claude-code", help="agent identity for lane scoping")
    cs.set_defaults(func=_run_claude_setup)
    ch = cl_sub.add_parser("hook", help="hook entry point (invoked by Claude Code, not by hand)")
    ch.add_argument("event", choices=["session-start", "user-prompt", "stop"])
    ch.set_defaults(func=_run_claude_hook)
    cu = cl_sub.add_parser("uninstall", help="remove the engram hooks from Claude Code settings")
    cu.set_defaults(func=_run_claude_uninstall)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
