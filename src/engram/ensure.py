"""Start-or-reuse for `engram serve`: the front door for short-lived clients.

Long-lived hosts (the OpenClaw gateway) spawn the daemon and hold its
startup contract in memory. Hook-style clients live for under a second and
cannot do that, so the daemon persists its contract to a state file next to
the store (`<db>.serve.json`, 0600) and `ensure()` makes "give me a live
daemon for this db" idempotent:

    state = ensure("~/.engram/memory.db")   # reuse or spawn, then verify

Liveness is decided by the daemon itself, not by pid bookkeeping: a state
file only counts if a tokened GET /health answers for the same db. A pid
probe is just the fast path to skip obviously-dead entries. Concurrent
ensure() calls arbitrate through an O_EXCL lock file; a lock whose holder
died is stolen after a grace period.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, Optional

SPAWN_TIMEOUT_S = 180.0     # a true first run may download the embedding model
LOCK_STALE_S = 15 * 60
HEALTH_TIMEOUT_S = 2.0


def state_path(db_path: str) -> str:
    return os.path.expanduser(db_path) + ".serve.json"


def _lock_path(db_path: str) -> str:
    return os.path.expanduser(db_path) + ".serve.lock"


def read_state(db_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(state_path(db_path), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if not all(k in raw for k in ("port", "token", "db", "pid")):
        return None
    return raw


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def is_live(state: Dict[str, Any], db_path: str) -> bool:
    """The authoritative probe: the daemon answers /health with this token
    for this db. Everything else is bookkeeping."""
    if not _pid_alive(int(state.get("pid", -1))):
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}/health",
        headers={"Authorization": f"Bearer {state['token']}"} if state.get("token") else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_S) as res:
            health = json.loads(res.read())
    except Exception:
        return False
    return bool(health.get("ok")) and os.path.expanduser(db_path) == state.get("db")


def _try_lock(db_path: str) -> bool:
    lock = _lock_path(db_path)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Steal only from the dead — and steal ATOMICALLY: rename first, so
        # exactly one contender wins even if several judge the same corpse.
        # An unreadable/empty lock younger than a beat is a holder mid-write,
        # not a corpse (the O_EXCL create and the json.dump are two steps).
        try:
            with open(lock, "r", encoding="utf-8") as f:
                holder = json.load(f)
            holder_dead = not _pid_alive(int(holder.get("pid", -1)))
            holder_old = time.time() - float(holder.get("ts", 0)) > LOCK_STALE_S
        except (OSError, ValueError):
            try:
                young = time.time() - os.path.getmtime(lock) < 10.0
            except OSError:
                return _try_lock(db_path)   # vanished — retry the clean path
            if young:
                return False                # mid-write holder, not stale
            holder_dead, holder_old = True, True
        if not (holder_dead or holder_old):
            return False
        grave = f"{lock}.stale.{os.getpid()}.{time.monotonic_ns()}"
        try:
            os.rename(lock, grave)          # atomic: one stealer wins
        except OSError:
            return _try_lock(db_path)       # lost the steal — re-evaluate
        try:
            os.unlink(grave)
        except OSError:
            pass
        return _try_lock(db_path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    return True


def _unlock(db_path: str) -> None:
    # release only what we hold: never delete another contender's fresh lock
    lock = _lock_path(db_path)
    try:
        with open(lock, "r", encoding="utf-8") as f:
            if json.load(f).get("pid") != os.getpid():
                return
        os.unlink(lock)
    except (OSError, ValueError):
        pass


LOG_ROTATE_BYTES = 5_000_000


def _spawn_detached(db_path: str, agent: str) -> "subprocess.Popen":
    db = os.path.expanduser(db_path)
    parent = os.path.dirname(db) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    log_path = db + ".serve.log"
    try:
        if os.path.getsize(log_path) > LOG_ROTATE_BYTES:
            os.replace(log_path, log_path + ".1")   # one rotated generation
    except OSError:
        pass
    log = open(log_path, "ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "engram.cli.main", "serve",
             "--db", db, "--port", "0", "--agent", agent,
             "--state", state_path(db)],
            stdout=subprocess.DEVNULL,   # the contract lives in the state file
            stderr=log,                  # structured logs survive for debugging
            stdin=subprocess.DEVNULL,
            start_new_session=True,      # survives the ensure() caller exiting
        )
    finally:
        log.close()


def ensure(db_path: str, agent: str = "default",
           timeout_s: float = SPAWN_TIMEOUT_S) -> Dict[str, Any]:
    """Return the contract of a verified-live daemon for db_path, spawning
    one if needed. Raises TimeoutError if none becomes healthy in time."""
    state = read_state(db_path)
    if state and is_live(state, db_path):
        return state

    deadline = time.time() + timeout_s
    proc = None
    death_grace = None
    while time.time() < deadline:
        state = read_state(db_path)
        if state and is_live(state, db_path):
            if proc is not None:
                _unlock(db_path)
            return state
        if proc is None:
            if _try_lock(db_path):
                # check once more inside the lock — someone may have won the race
                state = read_state(db_path)
                if state and is_live(state, db_path):
                    _unlock(db_path)
                    return state
                proc = _spawn_detached(db_path, agent)
            # if we didn't get the lock, another ensure() is spawning: poll
        elif proc.poll() is not None:
            # our spawn exited. Either it lost the daemon-side owner flock to
            # a sibling (whose state will appear shortly) or it genuinely
            # crashed — give the sibling a short grace, then fail fast instead
            # of burning the whole deadline.
            if death_grace is None:
                death_grace = time.time() + 5.0
            elif time.time() > death_grace:
                _unlock(db_path)
                raise TimeoutError(
                    f"the spawned engram daemon for {db_path} exited "
                    f"(code {proc.returncode}) and no other daemon appeared "
                    f"— see {os.path.expanduser(db_path)}.serve.log")
        time.sleep(0.25)

    if proc is not None:
        _unlock(db_path)
    raise TimeoutError(
        f"no engram daemon became healthy for {db_path} within {timeout_s:.0f}s "
        f"(see {os.path.expanduser(db_path)}.serve.log)"
    )
