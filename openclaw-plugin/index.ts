/**
 * Engram Memory — deterministic long-term memory for OpenClaw.
 *
 * Architecture: this plugin is a thin supervisor and client. It spawns
 * `engram serve` (a loopback-only Python daemon), and every memory decision
 * — what to keep, what to merge, what to serve, what to abstain from — is
 * made by that engine and recorded in its decision ledger. The TypeScript
 * layer never grows opinions of its own.
 *
 * Everything stays on this machine: the daemon binds 127.0.0.1 and refuses
 * anything else, requires a bearer token it mints per boot, and makes no
 * network calls of any kind. No embedding API, no cloud, no keys.
 */

import { isAbsolute } from "node:path";

import type { AgentToolResult } from "openclaw/plugin-sdk/agent-core";
import { definePluginEntry, type OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";

import type { EngramClient, LedgerDecision, SearchHit } from "./src/client.ts";
import { clampInt, expandTilde, parseConfig } from "./src/config.ts";
import { buildRecallContext, captureUserTurns } from "./src/hooks.ts";
import { SidecarManager } from "./src/sidecar.ts";
import { runSetup, runStatus, spawnHostRunner, type WizardOptions } from "./src/wizard.ts";

/** The slice of commander this plugin touches when wiring subcommands. */
type CommanderCommand = {
  command(name: string): CommanderCommand;
  description(text: string): CommanderCommand;
  action(handler: () => void | Promise<void>): CommanderCommand;
};

function textResult(text: string, details?: unknown): AgentToolResult {
  const result: AgentToolResult = { content: [{ type: "text", text }] };
  if (details !== undefined) result.details = details;
  return result;
}

function unavailableResult(): AgentToolResult {
  return textResult(
    "Memory is unavailable right now (the local engram sidecar is starting or down). " +
      "The agent can continue without it.",
  );
}

function formatSearchResult(hits: SearchHit[]): string {
  if (hits.length === 0) {
    return (
      "No stored memory matched in this agent's lane. " +
      "(Abstained rather than guessed — memory_diagnose shows recent serve decisions.)"
    );
  }
  return hits
    .map((h, i) => {
      const promotion = h.promotion ? ` [${h.promotion}]` : "";
      return `${i + 1}.${promotion} ${h.value}`;
    })
    .join("\n");
}

function formatDecisions(decisions: LedgerDecision[]): string {
  if (decisions.length === 0) return "No ledger entries recorded yet.";
  return decisions.map((d) => JSON.stringify(d)).join("\n");
}

export default definePluginEntry({
  id: "engram",
  name: "Engram Memory",
  description:
    "Deterministic long-term memory served from a local sidecar: role-scoped recall, " +
    "zero cloud calls, and a decision ledger that explains every keep, merge, and drop.",
  kind: "memory" as const,

  register(api: OpenClawPluginApi) {
    const rawConfig = api.pluginConfig ?? {};

    // CLI first, before any config gate: `openclaw engram setup` must work
    // precisely when the plugin is not yet configured.
    const wizardOptions = (): WizardOptions => ({
      ...(typeof rawConfig["dbPath"] === "string" && rawConfig["dbPath"].trim()
        ? { dbPath: rawConfig["dbPath"].trim() }
        : {}),
      ...(typeof rawConfig["agentId"] === "string" && rawConfig["agentId"].trim()
        ? { agentId: rawConfig["agentId"].trim() }
        : {}),
      ...(typeof rawConfig["pythonPath"] === "string" && rawConfig["pythonPath"].trim()
        ? { pythonPath: rawConfig["pythonPath"].trim() }
        : {}),
    });
    api.registerCli?.(
      ({ program }) => {
        const root = (program as CommanderCommand)
          .command("engram")
          .description("Engram memory: setup and status");
        root
          .command("setup")
          .description("One-question setup: persona in, memory slot configured")
          .action(async () => {
            const readline = await import("node:readline/promises");
            const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
            try {
              const code = await runSetup(
                {
                  ask: (q) => rl.question(q),
                  print: (line) => console.log(line),
                  runHost: spawnHostRunner(),
                },
                wizardOptions(),
              );
              process.exitCode = code;
            } finally {
              rl.close();
            }
          });
        root
          .command("status")
          .description("Engine health, store stats, and the last ledger decisions")
          .action(async () => {
            const code = await runStatus(
              {
                ask: async () => "",
                print: (line) => console.log(line),
                runHost: spawnHostRunner(),
              },
              wizardOptions(),
            );
            process.exitCode = code;
          });
      },
      { commands: ["engram"] },
    );

    const parsed = parseConfig(rawConfig);
    if (!parsed.ok) {
      const reason = parsed.error;
      api.registerService({
        id: "engram",
        start: () => {
          api.logger.warn(`engram: disabled until configured (${reason})`);
        },
      });
      return;
    }
    const cfg = parsed.cfg;

    const tilded = expandTilde(cfg.dbPath);
    const dbPath = isAbsolute(tilded) ? tilded : api.resolvePath(tilded);

    const manager = new SidecarManager({
      dbPath,
      agent: cfg.agentId,
      ...(cfg.pythonPath ? { pythonPath: cfg.pythonPath } : {}),
      ...(cfg.startupTimeoutMs ? { startupTimeoutMs: cfg.startupTimeoutMs } : {}),
      allowDownload: cfg.allowDownload,
      logger: api.logger,
      // Runs on EVERY successful start — initial, crash-restart, or retried
      // boot failure — so the profile is always registered (idempotent).
      onUp: async (handle) => {
        await handle.client.profile({ persona: cfg.persona, agent: cfg.agentId });
        api.logger.info(
          `engram: profile registered for agent "${cfg.agentId}" (persona: ${cfg.persona})`,
        );
      },
    });

    const bootedSessions = new Set<string>();
    const captureCursors = new Map<string, number>();
    const requireClient = (): EngramClient | null => manager.client();

    api.registerMemoryCapability?.({
      publicArtifacts: {
        async listArtifacts(params: unknown) {
          const hostCore = (await import(
            "openclaw/plugin-sdk/memory-host-core"
          )) as { listMemoryHostPublicArtifacts(p: unknown): Promise<unknown> };
          return await hostCore.listMemoryHostPublicArtifacts(params);
        },
      },
    });

    // ── tools ────────────────────────────────────────────────────────────────

    api.registerTool({
      name: "memory_search",
      label: "Memory Search",
      description:
        "Search this agent's long-term memory before answering questions about prior " +
        "work, decisions, dates, people, or preferences. Results are scoped to the " +
        "agent's lane; an empty result means memory abstained rather than guessed.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "What to look up" },
          k: { type: "integer", minimum: 1, maximum: 25, description: "Max results (default 5)" },
        },
        required: ["query"],
        additionalProperties: false,
      },
      execute: async (_toolCallId, params) => {
        const client = requireClient();
        if (!client) return unavailableResult();
        const query = typeof params["query"] === "string" ? params["query"] : "";
        if (!query.trim()) return textResult("memory_search needs a non-empty `query`.");
        const k = clampInt(params["k"], 5, 1, 25);
        try {
          const { hits } = await client.search(
            { query, agent: cfg.agentId, k },
            10_000,
          );
          return textResult(formatSearchResult(hits), { hits });
        } catch (err) {
          api.logger.warn(`engram: memory_search failed: ${String(err)}`);
          return unavailableResult();
        }
      },
    }, { name: "memory_search" });

    api.registerTool({
      name: "memory_store",
      label: "Memory Store",
      description:
        "Save a durable fact to this agent's long-term memory. The engine decides " +
        "deterministically whether to add, merge, or skip — and records why in the " +
        "decision ledger (see memory_diagnose).",
      parameters: {
        type: "object",
        properties: {
          text: { type: "string", description: "The fact to remember, one sentence" },
          tags: {
            type: "array",
            items: { type: "string" },
            maxItems: 10,
            description: "Optional topic tags",
          },
        },
        required: ["text"],
        additionalProperties: false,
      },
      execute: async (_toolCallId, params) => {
        const client = requireClient();
        if (!client) return unavailableResult();
        const text = typeof params["text"] === "string" ? params["text"] : "";
        if (!text.trim()) return textResult("memory_store needs a non-empty `text`.");
        const tags = Array.isArray(params["tags"])
          ? params["tags"].filter((t): t is string => typeof t === "string").slice(0, 10)
          : undefined;
        try {
          const res = await client.turn(
            { text, speaker: "user", ...(tags ? { tags } : {}) },
            10_000,
          );
          const decision = typeof res.decision === "string" ? res.decision : "UNKNOWN";
          return textResult(
            `Engine decision: ${decision}. memory_diagnose explains the reasoning trail.`,
            res,
          );
        } catch (err) {
          api.logger.warn(`engram: memory_store failed: ${String(err)}`);
          return unavailableResult();
        }
      },
    }, { name: "memory_store" });

    api.registerTool({
      name: "memory_diagnose",
      label: "Memory Diagnose",
      description:
        "Read the memory decision ledger: why items were kept, merged, skipped, or " +
        "truncated, and why recall served or abstained. Use when memory behavior " +
        "needs explaining — this is the audit trail, not a search.",
      parameters: {
        type: "object",
        properties: {
          kind: {
            type: "string",
            description: "Optional filter, e.g. capture-skip, merge, serve, truncation",
          },
          limit: { type: "integer", minimum: 1, maximum: 100, description: "Max entries (default 20)" },
        },
        additionalProperties: false,
      },
      execute: async (_toolCallId, params) => {
        const client = requireClient();
        if (!client) return unavailableResult();
        const kind = typeof params["kind"] === "string" ? params["kind"] : undefined;
        const limit = clampInt(params["limit"], 20, 1, 100);
        try {
          const { decisions } = await client.diagnose(
            { ...(kind ? { kind } : {}), limit },
            10_000,
          );
          return textResult(formatDecisions(decisions), { decisions });
        } catch (err) {
          api.logger.warn(`engram: memory_diagnose failed: ${String(err)}`);
          return unavailableResult();
        }
      },
    }, { name: "memory_diagnose" });

    // ── lifecycle hooks ──────────────────────────────────────────────────────

    api.on("before_prompt_build", async (event, ctx) => {
      if (!cfg.autoRecall) return undefined;
      const prompt = typeof event.prompt === "string" ? event.prompt : "";
      if (prompt.length < 5) return undefined;
      const client = requireClient();
      if (!client) return undefined; // still starting or down: never stall the agent
      try {
        const context = await buildRecallContext({
          client,
          agent: cfg.agentId,
          prompt,
          topK: cfg.topK,
          bootK: cfg.bootK,
          sessionKey: ctx?.sessionKey ?? ctx?.sessionId ?? "global",
          bootedSessions,
        });
        return context ? { prependContext: context } : undefined;
      } catch (err) {
        api.logger.warn(`engram: recall failed: ${String(err)}`);
        return undefined;
      }
    });

    api.on("agent_end", async (event, ctx) => {
      if (!cfg.autoCapture) return;
      // Reference semantics: missing `success` means skip — never capture
      // from a run the host did not mark successful.
      if (!event.success || !Array.isArray(event.messages) || event.messages.length === 0) {
        return;
      }
      const client = requireClient();
      if (!client) return;
      try {
        const outcome = await captureUserTurns({
          client,
          messages: event.messages,
          sessionKey: ctx.sessionKey ?? ctx.sessionId ?? "global",
          cursors: captureCursors,
        });
        if (outcome.stored > 0 || outcome.remaining > 0) {
          api.logger.info(
            `engram: captured ${outcome.stored}/${outcome.attempted} user turn(s)` +
              (outcome.remaining > 0
                ? `; ${outcome.remaining} message(s) resume next run`
                : ""),
          );
        }
      } catch (err) {
        api.logger.warn(`engram: capture failed: ${String(err)}`);
      }
    });

    api.on("session_end", (event, ctx) => {
      // "global" is the fallback key used when a hook fires without ctx —
      // clearing it here re-arms the boot snapshot even on hosts that never
      // provide a session identity to before_prompt_build.
      const keys = [
        ctx.sessionKey, ctx.sessionId,
        event.sessionKey, event.sessionId,
        event.nextSessionKey, event.nextSessionId,
        "global",
      ];
      for (const key of keys) {
        if (key) {
          captureCursors.delete(key);
          bootedSessions.delete(key);
        }
      }
    });

    // ── service lifecycle ────────────────────────────────────────────────────

    api.registerService({
      id: "engram",
      start: () => {
        api.logger.info(`engram: starting memory sidecar (db: ${dbPath})`);
        // Fire-and-forget: a first run may download the embedding model and
        // gateway boot must not wait on that. Hooks fail open until ready;
        // the manager retries failed starts on the same budget as crashes.
        void manager.start().catch((err) => {
          api.logger.warn(`engram: sidecar start failed: ${String(err)}`);
        });
      },
      stop: async () => {
        await manager.stop();
        api.logger.info("engram: sidecar stopped, store checkpointed");
      },
    });
  },
});
