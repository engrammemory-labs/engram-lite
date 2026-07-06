"""`engram serve` — the local memory daemon.

One long-lived process, one store, one loopback socket. Agent frameworks in
any language (the OpenClaw TypeScript plugin is the first) talk to the engine
over localhost HTTP instead of embedding Python. Think of it as the local
daemon pattern: every app on the machine shares one memory service.

Security stance, deliberately rigid:
  - binds 127.0.0.1 and refuses anything else — there is no flag to expose
    it to a network, because a personal memory store must never be one;
  - bearer-token auth ON by default: the token is auto-generated and printed
    once in the startup line, so only the process that spawned the daemon
    (or a user reading their own terminal) can talk to it. `--no-token`
    exists for local experimentation only;
  - request bodies capped (1 MB) and parsed strictly; errors return JSON,
    never tracebacks.

Supervisor contract: the FIRST stdout line is a single JSON object —

    {"engram_serve": {"port": 49213, "token": "…", "db": "...", "pid": 123,
                      "version": "0.1.0"}}

then stdout stays silent. Structured request logs (one JSON per line) go to
stderr. SIGTERM/SIGINT close the store cleanly. `--port 0` picks a free port.

Zero new dependencies: Python's stdlib ThreadingHTTPServer is enough for a
single-user loopback API, keeps the supply chain auditable, and gives
security scanners nothing to flag. The engine's re-entrant lock (red-team
verified under process/thread hammering) makes concurrent requests safe.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import profiles
from .core.memory import Memory

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000   # a memory turn is text; anything bigger is a dump
LOOPBACK = "127.0.0.1"
SOCKET_TIMEOUT_S = 30        # a client that stalls mid-request is dropped, so a
                            # half-open connection can never wedge a thread


class _Server(ThreadingHTTPServer):
    # daemon_threads: request threads die with the process, so SIGTERM stops
    # the daemon instantly even mid-write instead of blocking on in-flight
    # requests (red-team: SIGTERM under load hung the process).
    daemon_threads = True
    allow_reuse_address = True


class _Logger:
    """Best-effort structured logging on a bounded queue.

    A memory daemon must NEVER block on its log sink: if a supervisor stops
    draining our stderr, a synchronous write blocks on the full pipe and every
    memory operation freezes behind it. So request handlers only ENQUEUE (drop
    on overflow); one background thread drains to stderr. Losing a diagnostic
    line is always better than wedging the store.
    """

    def __init__(self, maxlines: int = 2000):
        self._q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=maxlines)
        self._t = threading.Thread(target=self._drain, name="engram-log", daemon=True)
        self._t.start()

    def emit(self, obj: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(json.dumps(obj))
        except queue.Full:
            pass   # consumer stalled — drop the line, never block the request

    def _drain(self) -> None:
        while True:
            line = self._q.get()
            if line is None:
                return
            try:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            except Exception:
                pass   # a broken stderr must not kill the logger thread

    def close(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._t.join(timeout=1.0)


class _State:
    """Everything the handlers need, built once at startup."""

    def __init__(self, mem: Memory, token: Optional[str], default_agent: str):
        self.mem = mem
        self.token = token
        self.default_agent = default_agent
        self.started = time.time()
        self.requests = 0
        self.log = _Logger()


class _BadRequest(Exception):
    """Raised by the request-boundary type checks; becomes a clean 400."""


def _req_text(body: Dict[str, Any], field: str) -> str:
    """A required, non-empty string field — or a clean 400. A non-string
    (int/list/dict) is a caller type error, never a traceback."""
    v = body.get(field)
    if not isinstance(v, str) or not v.strip():
        raise _BadRequest(f"'{field}' must be a non-empty string")
    return v


def _opt_int(v: Any, default: int, lo: int, hi: int, field: str) -> int:
    """An optional integer, clamped to [lo, hi]. Garbage → 400, not 500."""
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise _BadRequest(f"'{field}' must be an integer")
    return max(lo, min(n, hi))


class _Handler(BaseHTTPRequestHandler):
    server_version = "engram-serve"
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S   # drop stalled clients (slowloris protection)
    state: _State   # injected by serve()

    # ── plumbing ──────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):   # silence the default stderr chatter;
        pass                             # we emit our own structured line

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log(self, code: int, t0: float) -> None:
        self.state.requests += 1
        self.state.log.emit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": self.command, "path": self.path.split("?")[0],
            "status": code, "ms": round((time.time() - t0) * 1000, 1),
        })

    def _authed(self) -> bool:
        if self.state.token is None:
            return True
        got = self.headers.get("Authorization", "")
        return secrets.compare_digest(got, f"Bearer {self.state.token}")

    def _body(self) -> Optional[Dict[str, Any]]:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    # ── routing ───────────────────────────────────────────────────────────────
    def do_GET(self):   # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:
        t0 = time.time()
        url = urlparse(self.path)
        try:
            # oversized payloads are refused BEFORE reading: reply 413 and
            # close the connection (the unread body makes keep-alive unsafe)
            try:
                announced = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                announced = -1
            if announced > MAX_BODY_BYTES or announced < 0:
                self.close_connection = True
                self._send(413, {"error": f"body exceeds {MAX_BODY_BYTES} bytes"})
                return self._log(413, t0)
            if not self._authed():
                self._send(401, {"error": "missing or invalid bearer token"})
                return self._log(401, t0)
            handler = {
                ("GET", "/health"): self._health,
                ("GET", "/boot"): self._boot,
                ("GET", "/diagnose"): self._diagnose,
                ("POST", "/profile"): self._profile,
                ("POST", "/turn"): self._turn,
                ("POST", "/search"): self._search,
                ("POST", "/shutdown"): self._shutdown,
            }.get((method, url.path))
            if handler is None:
                self._send(404, {"error": f"no route {method} {url.path}"})
                return self._log(404, t0)
            code, payload = handler(url)
            self._send(code, payload)
            self._log(code, t0)
        except _BadRequest as e:
            self._send(400, {"error": str(e)})   # caller type error, never a 500
            self._log(400, t0)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 — the daemon must never die on a request
            logger.exception("request failed")
            try:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
            self._log(500, t0)

    # ── endpoints ─────────────────────────────────────────────────────────────
    def _health(self, url) -> tuple:
        m = self.state.mem
        return 200, {
            "ok": True,
            "version": _version(),
            "embedder": getattr(m.embedder, "model_name", type(m.embedder).__name__),
            "dim": m.embedder.dim,
            "facts": len(m.all_current()),
            "profiles": [p["agent"] for p in m.profiles()],
            "uptime_s": round(time.time() - self.state.started, 1),
            "requests": self.state.requests,
        }

    def _need_body(self) -> Dict[str, Any]:
        body = self._body()
        if body is None:
            raise _BadRequest("body must be a JSON object within 1 MB")
        return body

    def _profile(self, url) -> tuple:
        body = self._need_body()
        persona = _req_text(body, "persona")
        agent = body.get("agent")
        agent = agent if isinstance(agent, str) and agent else self.state.default_agent
        domain, scope = body.get("domain"), body.get("scope_tags")
        domain = domain if isinstance(domain, str) and domain else None
        if isinstance(scope, str):
            scope = [t.strip() for t in scope.split(",") if t.strip()]
        elif not isinstance(scope, list):
            scope = None
        if not (domain and scope):
            d_domain, d_scope = profiles.derive_profile(persona)
            domain = domain or d_domain
            scope = scope or d_scope
        res = self.state.mem.register_profile(agent, persona, domain, [str(t) for t in scope])
        return 200, res

    def _turn(self, url) -> tuple:
        body = self._need_body()
        text = _req_text(body, "text")
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, (list, str)):
            raise _BadRequest("'tags' must be a list or comma-string")
        for opt in ("speaker", "when"):
            if body.get(opt) is not None and not isinstance(body[opt], str):
                raise _BadRequest(f"'{opt}' must be a string")
        res = self.state.mem.remember(text, tags=tags,
                                      speaker=body.get("speaker"),
                                      when=body.get("when"))
        return 200, res

    def _search(self, url) -> tuple:
        body = self._need_body()
        query = _req_text(body, "query")
        agent = body.get("agent")
        agent = agent if isinstance(agent, str) and agent else self.state.default_agent
        tags = body.get("task_tags")
        if tags is not None and not isinstance(tags, (list, str)):
            raise _BadRequest("'task_tags' must be a list or comma-string")
        k = _opt_int(body.get("k"), 5, 1, 200, "k")
        hits = self.state.mem.search(query, agent=agent, task_tags=tags, k=k)
        return 200, {"hits": [
            {"value": h["value"], "id": h["id"], "tags": h.get("tags"),
             "promotion": h.get("promotion")} for h in hits]}

    def _boot(self, url) -> tuple:
        q = parse_qs(url.query)
        agent = (q.get("agent") or [self.state.default_agent])[0]
        k = _opt_int((q.get("k") or [None])[0], 8, 1, 50, "k")
        hits = self.state.mem.lane_snapshot(agent, k=k)
        return 200, {"memories": [h["value"] for h in hits]}

    def _diagnose(self, url) -> tuple:
        q = parse_qs(url.query)
        kind = (q.get("kind") or [None])[0]
        limit = _opt_int((q.get("limit") or [None])[0], 20, 1, 100, "limit")
        rows = self.state.mem.decisions(kind=kind, limit=limit)
        return 200, {"decisions": rows}

    def _shutdown(self, url) -> tuple:
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return 200, {"ok": True, "stopping": True}


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("engram-lite")
    except Exception:
        return "dev"


def serve(db_path: str, port: int = 0, token: Optional[str] = "auto",
          agent: str = "default", host: str = LOOPBACK,
          state_file: Optional[str] = None) -> None:
    """Run the daemon until SIGTERM/SIGINT or POST /shutdown."""
    if host != LOOPBACK:
        # a personal memory store must never be a network service. There is
        # deliberately no override: bind loopback or don't run.
        sys.exit(f"engram serve binds {LOOPBACK} only (got {host!r}). "
                 "Exposing a personal memory store to a network is not supported.")
    resolved_token = secrets.token_urlsafe(24) if token == "auto" else token

    # ONE daemon per store, enforced by the kernel: an exclusive flock held
    # for our whole lifetime. Redundant spawns (racing ensure() callers,
    # supervisors retrying) block briefly for a restart overlap, then exit —
    # they can never produce a second daemon, a clobbered state file, or an
    # orphaned process. The lock dies with us; no stale-pid judging needed.
    import fcntl
    owner_path = os.path.expanduser(db_path) + ".owner.lock"
    os.makedirs(os.path.dirname(owner_path) or ".", exist_ok=True)
    owner_fd = os.open(owner_path, os.O_CREAT | os.O_WRONLY, 0o600)
    owner_wait = float(os.environ.get("ENGRAM_OWNER_WAIT_S", "30"))
    deadline = time.time() + owner_wait   # a predecessor may be checkpointing its WAL
    while True:
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                sys.exit(f"another engram daemon owns {db_path} "
                         "(its .owner.lock is held); not starting a second one.")
            time.sleep(0.25)

    mem = Memory(os.path.expanduser(db_path), origin_tool=agent)
    state = _State(mem, resolved_token, agent)
    _Handler.state = state

    httpd = _Server((LOOPBACK, port), _Handler)
    actual_port = httpd.server_address[1]

    contract = {
        "port": actual_port,
        "token": resolved_token,
        "db": os.path.expanduser(db_path),
        "pid": os.getpid(),
        "version": _version(),
    }

    # the supervisor contract: FIRST stdout line is the connection info
    print(json.dumps({"engram_serve": contract}), flush=True)

    # short-lived clients (hooks) find the daemon through a state file the
    # daemon itself owns: written atomically at 0600 after bind, removed on
    # clean shutdown if it is still ours
    if state_file:
        # unique-per-attempt tmp name: pid alone can recur (pid reuse), and a
        # crash between create and replace must never mine a future daemon
        tmp = f"{state_file}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(contract, f)
        os.replace(tmp, state_file)

    def _stop(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        mem.close()          # checkpoint the WAL; the store is safe on disk
        if state_file:
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    if json.load(f).get("pid") == os.getpid():
                        os.unlink(state_file)
            except (OSError, ValueError):
                pass         # someone else's state file (or already gone)
        state.log.emit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "event": "engram serve stopped"})
        state.log.close()    # flush what we can, then let the process exit
