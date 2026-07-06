/**
 * Plugin configuration: one required question, defaults for everything else.
 *
 * SDK-free so the wizard, the entry point, and the tests all share one
 * parser — the same "one brain" rule the engine applies to persona
 * derivation.
 */

import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_DB_PATH = "~/.engram/openclaw.db";
export const DEFAULT_AGENT_ID = "openclaw";

export type EngramConfig = {
  persona: string;
  agentId: string;
  dbPath: string;
  pythonPath?: string;
  autoRecall: boolean;
  autoCapture: boolean;
  /** Opt-in: let uvx/pipx fetch the version-pinned engine. Default false. */
  allowDownload: boolean;
  topK: number;
  bootK: number;
  startupTimeoutMs?: number;
};

export type ConfigResult = { ok: true; cfg: EngramConfig } | { ok: false; error: string };

export function expandTilde(path: string): string {
  if (path === "~") return homedir();
  if (path.startsWith("~/")) return join(homedir(), path.slice(2));
  return path;
}

export function clampInt(value: unknown, fallback: number, lo: number, hi: number): number {
  const n = typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : fallback;
  return Math.min(hi, Math.max(lo, n));
}

export function parseConfig(raw: Record<string, unknown>): ConfigResult {
  const persona = raw["persona"];
  if (typeof persona !== "string" || persona.trim().length < 2) {
    return {
      ok: false,
      error:
        "set `persona` in the plugin config (one sentence: what is this agent?) — " +
        "run `openclaw engram setup`, which asks exactly that one question",
    };
  }
  for (const key of ["dbPath", "agentId", "pythonPath"] as const) {
    if (raw[key] !== undefined && typeof raw[key] !== "string") {
      return { ok: false, error: `\`${key}\` must be a string` };
    }
  }
  const cfg: EngramConfig = {
    persona: persona.trim(),
    agentId:
      typeof raw["agentId"] === "string" && raw["agentId"].trim()
        ? raw["agentId"].trim()
        : DEFAULT_AGENT_ID,
    dbPath:
      typeof raw["dbPath"] === "string" && raw["dbPath"].trim()
        ? raw["dbPath"].trim()
        : DEFAULT_DB_PATH,
    autoRecall: raw["autoRecall"] !== false,
    autoCapture: raw["autoCapture"] !== false,
    allowDownload: raw["allowDownload"] === true,
    topK: clampInt(raw["topK"], 4, 1, 10),
    bootK: clampInt(raw["bootK"], 6, 0, 20),
  };
  if (typeof raw["pythonPath"] === "string" && raw["pythonPath"].trim()) {
    cfg.pythonPath = raw["pythonPath"].trim();
  }
  if (raw["startupTimeoutMs"] !== undefined) {
    cfg.startupTimeoutMs = clampInt(raw["startupTimeoutMs"], 120_000, 5_000, 600_000);
  }
  return { ok: true, cfg };
}
