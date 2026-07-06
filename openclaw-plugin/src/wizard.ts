/**
 * `openclaw engram setup` and `openclaw engram status`.
 *
 * The wizard asks exactly one question — what is this agent? — and derives
 * everything else. Config writes are delegated to the host's own writer
 * (`openclaw config set`) so the user's config file is never touched by
 * this code; if the host writer is unavailable, the wizard prints the exact
 * block to paste instead of guessing at file formats.
 *
 * Both commands verify against the REAL engine: they start an ephemeral
 * sidecar with the same launcher ladder the plugin uses at runtime, so a
 * passing setup means the runtime path works — not that a mock did.
 *
 * All effects are injected (ask/print/runHost) to keep this testable
 * without a host or a TTY.
 */

import { spawn } from "node:child_process";

import { readFileSync } from "node:fs";

import { EngramClient } from "./client.ts";
import { DEFAULT_AGENT_ID, DEFAULT_DB_PATH, expandTilde } from "./config.ts";
import { startSidecar, type SidecarHandle } from "./sidecar.ts";

export type WizardIO = {
  ask(question: string): Promise<string>;
  print(line: string): void;
  /** Run the host CLI (`openclaw <args>`); resolves with its exit code. */
  runHost(args: string[]): Promise<{ code: number; output: string }>;
};

export type WizardOptions = {
  dbPath?: string;
  agentId?: string;
  pythonPath?: string;
  env?: Record<string, string | undefined>;
};

function resolveTargets(opts: WizardOptions): { dbPath: string; agentId: string } {
  return {
    dbPath: expandTilde(opts.dbPath ?? DEFAULT_DB_PATH),
    agentId: opts.agentId ?? DEFAULT_AGENT_ID,
  };
}

/** The daemon enforces one-instance-per-store (an owner flock), so the
 * wizard must REUSE a live daemon — e.g. the gateway's — and only spawn an
 * ephemeral one when the store has no daemon at all. Discovery is the same
 * state-file protocol every adapter uses. */
async function liveClient(dbPath: string): Promise<EngramClient | null> {
  let state: { port?: number; token?: string | null; db?: string };
  try {
    state = JSON.parse(readFileSync(`${dbPath}.serve.json`, "utf-8"));
  } catch {
    return null;
  }
  if (typeof state.port !== "number" || state.db !== dbPath) return null;
  const client = new EngramClient(state.port, state.token ?? null);
  try {
    const health = await client.health(1_500);
    return health.ok ? client : null;
  } catch {
    return null;
  }
}

async function withEphemeralSidecar<T>(
  opts: WizardOptions,
  dbPath: string,
  agentId: string,
  work: (handle: { client: EngramClient; info: { version: string } }) => Promise<T>,
): Promise<T> {
  const reused = await liveClient(dbPath);
  if (reused) {
    const health = await reused.health();
    return await work({ client: reused, info: { version: health.version } });
  }
  const handle = await startSidecar({
    dbPath,
    agent: agentId,
    ...(opts.pythonPath ? { pythonPath: opts.pythonPath } : {}),
    ...(opts.env ? { env: opts.env } : {}),
  });
  try {
    return await work(handle);
  } finally {
    await handle.stop();
  }
}

/** The default runHost: spawn the host CLI without a shell. */
export function spawnHostRunner(): WizardIO["runHost"] {
  return (args) =>
    new Promise((resolve) => {
      const proc = spawn("openclaw", args, { stdio: ["ignore", "pipe", "pipe"] });
      let output = "";
      proc.stdout?.on("data", (c: Buffer) => (output += c.toString()));
      proc.stderr?.on("data", (c: Buffer) => (output += c.toString()));
      proc.once("error", () => resolve({ code: 127, output: "openclaw CLI not found" }));
      proc.once("exit", (code) => resolve({ code: code ?? 1, output }));
    });
}

const MANUAL_BLOCK = (persona: string) => `  {
    "plugins": {
      "slots": { "memory": "engram" },
      "entries": {
        "engram": { "enabled": true, "config": { "persona": ${JSON.stringify(persona)} } }
      }
    }
  }`;

export async function runSetup(io: WizardIO, opts: WizardOptions = {}): Promise<number> {
  const { dbPath, agentId } = resolveTargets(opts);

  // 1. One question.
  let persona = "";
  for (let attempt = 0; attempt < 3 && persona.length < 2; attempt++) {
    persona = (await io.ask("What is this agent? (one sentence, e.g. \"DevOps engineer\"): ")).trim();
  }
  if (persona.length < 2) {
    io.print("setup aborted: a persona is required — it is the only question.");
    return 1;
  }

  // 2. Prove the engine end to end: same launcher ladder as the runtime,
  //    real db, real profile registration. Failure here prints exactly
  //    which launchers were tried and how to install.
  io.print("Checking the memory engine (engram-lite)…");
  let derived: { domain?: unknown; scope_tags?: unknown };
  try {
    derived = await withEphemeralSidecar(opts, dbPath, agentId, async (handle) => {
      const profile = await handle.client.profile({ persona, agent: agentId });
      const health = await handle.client.health();
      io.print(
        `Engine OK (engram-lite v${handle.info.version}, embedder ${health.embedder}, ` +
          `store ${dbPath}, ${health.facts} facts).`,
      );
      return profile;
    });
  } catch (err) {
    io.print(String(err instanceof Error ? err.message : err));
    io.print("");
    io.print("Install the engine, then re-run `openclaw engram setup`:");
    io.print("  pip install engram-lite   (or: uv tool install engram-lite / pipx install engram-lite)");
    return 1;
  }
  const domain = typeof derived.domain === "string" ? derived.domain : "derived";
  const scope = Array.isArray(derived.scope_tags) ? derived.scope_tags.join(", ") : "";
  io.print(`Profile registered: agent "${agentId}" → domain "${domain}"${scope ? ` (scope: ${scope})` : ""}.`);

  // 3. Config via the host's own writer — never by editing files ourselves.
  const writes: Array<[string, string]> = [
    ["plugins.slots.memory", "engram"],
    ["plugins.entries.engram.enabled", "true"],
    ["plugins.entries.engram.config.persona", persona],
  ];
  let allWritten = true;
  for (const [key, value] of writes) {
    const res = await io.runHost(["config", "set", key, value]);
    if (res.code !== 0) {
      allWritten = false;
      io.print(`(config set ${key} failed: ${res.output.trim() || `exit ${res.code}`})`);
    }
  }
  if (allWritten) {
    io.print("Config written: engram now owns the memory slot.");
  } else {
    io.print("");
    io.print("Automatic config write failed — merge this into your OpenClaw config instead:");
    io.print(MANUAL_BLOCK(persona));
  }

  io.print("");
  io.print("Done. Restart the gateway to load the plugin, then verify with:");
  io.print("  openclaw engram status");
  return allWritten ? 0 : 2;
}

export async function runStatus(io: WizardIO, opts: WizardOptions = {}): Promise<number> {
  const { dbPath, agentId } = resolveTargets(opts);
  try {
    return await withEphemeralSidecar(opts, dbPath, agentId, async (handle) => {
      const health = await handle.client.health();
      const { decisions } = await handle.client.diagnose({ limit: 5 });
      io.print(`engram-lite v${handle.info.version} — store: ${dbPath}`);
      io.print(
        `facts: ${health.facts} · profiles: ${health.profiles.join(", ") || "(none yet)"} · ` +
          `embedder: ${health.embedder} (dim ${health.dim})`,
      );
      if (decisions.length) {
        io.print("last ledger decisions:");
        for (const d of decisions) io.print(`  ${JSON.stringify(d)}`);
      } else {
        io.print("ledger: no decisions recorded yet.");
      }
      return 0;
    });
  } catch (err) {
    io.print(String(err instanceof Error ? err.message : err));
    return 1;
  }
}
