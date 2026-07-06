"""Zero-egress proof: the full memory loop with the network physically gone.

Runs inside a container started with `--network none`. Everything the memory
layer does at runtime must work; every attempt to leave the machine must
fail. Exit code 0 = every check passed.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""),
          flush=True)


def call(port: int, token: str, method: str, route: str, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if body is not None else {})},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main() -> int:
    print("── zero-egress proof (runtime network: NONE) ──", flush=True)

    # 0. Prove the cage is real before crediting anything to the engine.
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        cage = False
    except OSError:
        cage = True
    check("environment has no egress (1.1.1.1:443 unreachable)", cage)
    try:
        socket.getaddrinfo("pypi.org", 443)
        dns = False
    except OSError:
        dns = True
    check("environment has no DNS (pypi.org unresolvable)", dns)
    if not (cage and dns):
        print("refusing to continue: the cage is not sealed, results would be meaningless")
        return 1

    # 1. Daemon starts offline — model must already be in the image.
    proc = subprocess.Popen(
        [sys.executable, "-m", "engram.cli.main", "serve",
         "--db", "/tmp/proof.db", "--port", "0", "--agent", "openclaw"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()
    t0 = time.time()
    line = proc.stdout.readline()
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
    try:
        info = json.loads(line)["engram_serve"]
    except Exception:
        check("daemon starts with no network", False, f"bad contract: {line[:120]!r}")
        proc.kill()
        return 1
    check("daemon starts with no network", True, f"{time.time() - t0:.1f}s, pid {info['pid']}")
    port, token = info["port"], info["token"]

    try:
        # 2. Real embedder loaded from the baked cache (not the hash stub).
        health = call(port, token, "GET", "/health")
        real_model = "bge" in str(health.get("embedder", "")).lower()
        check("real embedding model served from the baked cache", real_model,
              f"{health.get('embedder')} (dim {health.get('dim')})")

        # 3. Full memory roundtrip, all offline.
        prof = call(port, token, "POST", "/profile",
                    {"agent": "openclaw", "persona": "DevOps engineer"})
        check("profile derivation", prof.get("domain") == "sre-devops",
              f"domain={prof.get('domain')}")

        call(port, token, "POST", "/turn",
             {"text": "The canary threshold is 2 percent error rate", "speaker": "user"})
        call(port, token, "POST", "/turn",
             {"text": "The deploy freeze is every Friday", "speaker": "user"})

        hits = call(port, token, "POST", "/search",
                    {"query": "what is the canary threshold?", "agent": "openclaw"})["hits"]
        check("semantic search (real embeddings, offline)",
              any("2 percent" in h["value"] for h in hits), f"{len(hits)} hit(s)")

        boot = call(port, token, "GET", "/boot?agent=openclaw")["memories"]
        check("lane snapshot (boot block)", any("canary" in m for m in boot),
              f"{len(boot)} memories")

        diag = call(port, token, "GET", "/diagnose?limit=5")["decisions"]
        check("decision ledger readable", isinstance(diag, list))

        # 4. Wrong token still walled off, even in the cage.
        try:
            call(port, "wrong-token", "GET", "/health")
            check("bearer wall inside the sandbox", False)
        except urllib.error.HTTPError as e:
            check("bearer wall inside the sandbox", e.code == 401, f"HTTP {e.code}")
    finally:
        try:
            call(port, token, "POST", "/shutdown", {})
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
            check("clean shutdown (WAL checkpoint)", True, f"exit {proc.returncode}")
        except subprocess.TimeoutExpired:
            proc.kill()
            check("clean shutdown (WAL checkpoint)", False, "did not exit in 8s")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n  ZERO-EGRESS PROOF: {'ALL ' + str(len(RESULTS)) + ' CHECKS PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}",
          flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
