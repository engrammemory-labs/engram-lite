"""The `engram` command — dispatches to subcommands.

  engram demo      interactive REPL
  engram status    print current env settings
  engram rebuild   re-embed a store after changing the embedding model
"""
from __future__ import annotations

import argparse

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
        except sqlite3.Error:
            print("usage: (no memory yet)")
        finally:
            con.close()
    else:
        print("usage: (no DB at this path yet)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram", description="engram — local agentic memory")
    sub = parser.add_subparsers(dest="command")

    demo.add_parser(sub)
    sub.add_parser("status", help="print the current env settings").set_defaults(func=_run_status)
    sub.add_parser("selftest", help="verify the engine works (offline)").set_defaults(func=_run_selftest)
    rb = sub.add_parser("rebuild", help="re-embed a store after changing ENGRAM_EMBEDDER_MODEL")
    rb.add_argument("db", help="path to the memory .db file")
    rb.set_defaults(func=_run_rebuild)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
