/** Wizard flows: setup (real engine), failure paths, status. IO fully injected. */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { runSetup, runStatus, type WizardIO } from "../src/wizard.ts";
import { testPython } from "./helpers.ts";

type HostCall = string[];

function fakeIO(params: {
  answers?: string[];
  hostExit?: number;
}): { io: WizardIO; printed: string[]; hostCalls: HostCall[] } {
  const answers = [...(params.answers ?? [])];
  const printed: string[] = [];
  const hostCalls: HostCall[] = [];
  return {
    printed,
    hostCalls,
    io: {
      ask: async () => answers.shift() ?? "",
      print: (line) => printed.push(line),
      runHost: async (args) => {
        hostCalls.push(args);
        return { code: params.hostExit ?? 0, output: params.hostExit ? "boom" : "" };
      },
    },
  };
}

test("setup: one question → engine proven → profile derived → config written via host", async () => {
  const dir = mkdtempSync(join(tmpdir(), "engram-wizard-"));
  const dbPath = join(dir, "memory.db");
  const { io, printed, hostCalls } = fakeIO({ answers: ["DevOps engineer"] });
  try {
    const code = await runSetup(io, { dbPath, pythonPath: testPython(), env: { ENGRAM_EMBEDDER: "hash" } });
    assert.equal(code, 0);

    // the engine really ran: db exists, profile derivation surfaced
    assert.ok(existsSync(dbPath));
    const text = printed.join("\n");
    assert.match(text, /Engine OK/);
    assert.match(text, /sre-devops/); // derived domain, not asked

    // config written through the HOST writer, never by editing files
    assert.deepEqual(hostCalls, [
      ["config", "set", "plugins.slots.memory", "engram"],
      ["config", "set", "plugins.entries.engram.enabled", "true"],
      ["config", "set", "plugins.entries.engram.config.persona", "DevOps engineer"],
    ]);
    assert.match(text, /openclaw engram status/); // next step is stated
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("setup: empty persona after 3 asks aborts with exit 1 and no host writes", async () => {
  const { io, hostCalls } = fakeIO({ answers: ["", "  ", ""] });
  const code = await runSetup(io, { pythonPath: testPython(), env: { ENGRAM_EMBEDDER: "hash" } });
  assert.equal(code, 1);
  assert.equal(hostCalls.length, 0);
});

test("setup: missing engine → install hint, exit 1, no host writes", async () => {
  const { io, printed, hostCalls } = fakeIO({ answers: ["DevOps engineer"] });
  const code = await runSetup(io, {
    dbPath: join(tmpdir(), "never.db"),
    pythonPath: "/nonexistent/interpreter/python3",
    env: { ENGRAM_EMBEDDER: "hash" },
  });
  assert.equal(code, 1);
  assert.equal(hostCalls.length, 0);
  assert.match(printed.join("\n"), /pip install engram-lite/);
});

test("setup: host writer failing → manual config block fallback, exit 2", async () => {
  const dir = mkdtempSync(join(tmpdir(), "engram-wizard-fb-"));
  const { io, printed } = fakeIO({ answers: ["security engineer"], hostExit: 127 });
  try {
    const code = await runSetup(io, {
      dbPath: join(dir, "memory.db"),
      pythonPath: testPython(),
      env: { ENGRAM_EMBEDDER: "hash" },
    });
    assert.equal(code, 2);
    const text = printed.join("\n");
    assert.match(text, /merge this into your OpenClaw config/);
    assert.match(text, /"persona": "security engineer"/);
    assert.match(text, /"slots": \{ "memory": "engram" \}/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("status: reports store health and ledger over the real engine", async () => {
  const dir = mkdtempSync(join(tmpdir(), "engram-status-"));
  const dbPath = join(dir, "memory.db");
  const setupIO = fakeIO({ answers: ["backend engineer"] });
  try {
    assert.equal(await runSetup(setupIO.io, { dbPath, pythonPath: testPython(), env: { ENGRAM_EMBEDDER: "hash" } }), 0);

    const { io, printed } = fakeIO({});
    const code = await runStatus(io, { dbPath, pythonPath: testPython(), env: { ENGRAM_EMBEDDER: "hash" } });
    assert.equal(code, 0);
    const text = printed.join("\n");
    assert.match(text, /engram-lite v/);
    assert.match(text, /profiles: openclaw/);
    assert.match(text, /facts: \d+/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("status: unreachable engine degrades to exit 1 with the ladder error", async () => {
  const { io, printed } = fakeIO({});
  const code = await runStatus(io, {
    dbPath: join(tmpdir(), "never.db"),
    pythonPath: "/nonexistent/interpreter/python3",
    env: { ENGRAM_EMBEDDER: "hash" },
  });
  assert.equal(code, 1);
  assert.match(printed.join("\n"), /authoritative; not falling back/);
});
