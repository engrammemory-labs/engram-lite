/** Sidecar lifecycle against the real daemon: contract, health, crash-restart, ladder. */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { EngramHttpError, EngramClient } from "../src/client.ts";
import { launchCandidates, startSidecar, SidecarManager } from "../src/sidecar.ts";
import { launchDaemon, testPython } from "./helpers.ts";

test("startup contract, token auth, health, clean stop", async () => {
  const fixture = await launchDaemon();
  const { handle, dbPath } = fixture;
  try {
    // supervisor contract
    assert.ok(handle.info.port > 0);
    assert.ok(typeof handle.info.token === "string" && handle.info.token.length > 10);
    assert.ok(handle.info.pid > 0);
    assert.equal(handle.info.db, dbPath);

    // end-to-end health through the authed client
    const health = await handle.client.health();
    assert.equal(health.ok, true);
    assert.equal(health.facts, 0);

    // the token wall is real: wrong token → 401
    const impostor = new EngramClient(handle.info.port, "wrong-token");
    await assert.rejects(
      () => impostor.health(),
      (err: unknown) => err instanceof EngramHttpError && err.status === 401,
    );
  } finally {
    await fixture.cleanup();
  }
  // clean stop: process exited and the store file survived on disk
  assert.equal(handle.exited(), true);
});

test("store survives a stop/start cycle (restart durability)", async () => {
  const dir = mkdtempSync(join(tmpdir(), "engram-openclaw-restart-"));
  const dbPath = join(dir, "memory.db");
  try {
    const first = await startSidecar({
      dbPath,
      pythonPath: testPython(),
      env: { ENGRAM_EMBEDDER: "hash" },
    });
    await first.client.turn({ text: "The deploy freeze is every Friday" });
    await first.stop();
    assert.ok(existsSync(dbPath));

    const second = await startSidecar({
      dbPath,
      pythonPath: testPython(),
      env: { ENGRAM_EMBEDDER: "hash" },
    });
    try {
      const { hits } = await second.client.search({ query: "when is the deploy freeze?" });
      assert.ok(hits.some((h) => h.value.includes("Friday")));
    } finally {
      await second.stop();
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("manager restarts the sidecar after a hard crash", async () => {
  const dir = mkdtempSync(join(tmpdir(), "engram-openclaw-crash-"));
  const manager = new SidecarManager({
    dbPath: join(dir, "memory.db"),
    pythonPath: testPython(),
    env: { ENGRAM_EMBEDDER: "hash" },
  });
  try {
    const handle = await manager.start();
    const firstPid = handle.info.pid;
    process.kill(firstPid, "SIGKILL"); // simulate a hard crash

    // backoff is 1s for the first crash; poll until a NEW healthy pid appears
    let revived: number | null = null;
    for (let i = 0; i < 40 && revived === null; i++) {
      await new Promise((r) => setTimeout(r, 500));
      const current = manager.current();
      if (current && current.info.pid !== firstPid) {
        const health = await current.client.health().catch(() => null);
        if (health?.ok) revived = current.info.pid;
      }
    }
    assert.ok(revived !== null, "sidecar was not restarted after SIGKILL");
    assert.notEqual(revived, firstPid);
  } finally {
    await manager.stop();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("explicit pythonPath is authoritative: no silent fallback", async () => {
  await assert.rejects(
    () =>
      startSidecar({
        dbPath: join(tmpdir(), "never-created.db"),
        pythonPath: "/nonexistent/interpreter/python3",
      }),
    /authoritative; not falling back/,
  );
});

test("default ladder is offline-only and says how to opt in to downloads", async () => {
  await assert.rejects(
    () =>
      startSidecar({
        dbPath: join(tmpdir(), "never-created.db"),
        env: { PATH: "/nonexistent-dir", ENGRAM_PYTHON: undefined, ENGRAM_EMBEDDER: "hash" },
      }),
    (err: unknown) => {
      const message = String(err);
      return (
        message.includes("no launcher found") &&
        message.includes("engram") &&
        !message.includes("uvx:") && // download rungs NOT attempted by default
        message.includes("allowDownload")
      );
    },
  );
});

test("allowDownload=true adds the pinned uvx/pipx rungs to the ladder", async () => {
  await assert.rejects(
    () =>
      startSidecar({
        dbPath: join(tmpdir(), "never-created.db"),
        allowDownload: true,
        env: { PATH: "/nonexistent-dir", ENGRAM_PYTHON: undefined, ENGRAM_EMBEDDER: "hash" },
      }),
    (err: unknown) => {
      const message = String(err);
      return message.includes("uvx") && message.includes("pipx");
    },
  );
});

test("launchCandidates: explicit interpreter short-circuits, downloads are opt-in and pinned", () => {
  const explicit = launchCandidates({ dbPath: "/x.db", pythonPath: "/opt/py" });
  assert.equal(explicit.length, 1);
  assert.equal(explicit[0]?.cmd, "/opt/py");
  assert.deepEqual(explicit[0]?.args.slice(0, 2), ["-m", "engram.cli.main"]);

  // ENGRAM_PYTHON is honored from the provided env (wizard and runtime alike)
  const pinnedEnv = launchCandidates({ dbPath: "/x.db", env: { ENGRAM_PYTHON: "/opt/py2" } });
  assert.equal(pinnedEnv.length, 1);
  assert.equal(pinnedEnv[0]?.cmd, "/opt/py2");

  // default: offline rungs only — the plugin never downloads on its own
  const auto = launchCandidates({ dbPath: "/x.db", agent: "ops", env: { ENGRAM_PYTHON: undefined } });
  assert.deepEqual(auto.map((c) => c.kind), ["path"]);
  assert.ok(auto.every((c) => c.args.includes("--agent") && c.args.includes("ops")));

  // opted in: uvx/pipx appear, pinned to the exact engine release
  const withDl = launchCandidates({
    dbPath: "/x.db",
    allowDownload: true,
    env: { ENGRAM_PYTHON: undefined },
  });
  assert.deepEqual(withDl.map((c) => c.kind), ["path", "uvx", "pipx"]);
  const uvx = withDl.find((c) => c.kind === "uvx");
  assert.ok(uvx?.args.join(" ").includes("engram-lite==")); // version-pinned, never latest
});

test("manager: a failed initial start schedules a retry instead of dying forever", async () => {
  const warns: string[] = [];
  const manager = new SidecarManager({
    dbPath: join(tmpdir(), "never-created.db"),
    pythonPath: "/nonexistent/interpreter/python3",
    logger: { info: () => {}, warn: (m) => warns.push(m) },
  });
  await assert.rejects(() => manager.start());
  assert.ok(
    warns.some((w) => w.includes("retry 1/3")),
    `expected a scheduled retry, got: ${warns.join(" | ")}`,
  );
  await manager.stop(); // cancels the pending retry timer path
});

test("manager: stop() during an in-flight start aborts it and leaves no daemon", async () => {
  const dir = mkdtempSync(join(tmpdir(), "engram-abort-"));
  const manager = new SidecarManager({
    dbPath: join(dir, "memory.db"),
    pythonPath: testPython(),
    env: { ENGRAM_EMBEDDER: "hash" },
  });
  const inFlight = manager.start();
  await manager.stop(); // must not wait the spawn out; must clean up either way
  await inFlight.catch(() => {}); // aborted or completed — both fine
  assert.equal(manager.client(), null);
  rmSync(dir, { recursive: true, force: true });
});
