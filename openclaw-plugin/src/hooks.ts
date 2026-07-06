/**
 * Hook logic, kept SDK-free so it is testable against the real sidecar.
 *
 * Two invariants, both borrowed from how the daemon itself is built:
 *   - fail open: memory being down or slow yields `null` / zero counts,
 *     never an exception into the agent loop;
 *   - hard time budgets: recall shares the prompt-build critical path, so
 *     every call carries its own timeout and the whole hook is bounded.
 *
 * Capture is resumable, not lossy: the cursor advances only over messages
 * whose texts actually reached the engine (which then owns the keep/skip
 * decision in its ledger). Overflow beyond the per-run budget and
 * engine-unreachable failures leave the cursor behind, so the next
 * agent_end resumes exactly where this one stopped; the engine's merge
 * rules make redelivery safe.
 */

import { EngramHttpError, type EngramClient } from "./client.ts";
import { extractUserTexts } from "./capture.ts";
import { formatMemoryContext } from "./format.ts";

export type RecallParams = {
  client: EngramClient;
  agent: string;
  prompt: string;
  topK: number;
  bootK: number;
  sessionKey: string;
  bootedSessions: Set<string>;
  timeoutMs?: number;
};

const RECALL_TIMEOUT_MS = 1_500;
const CAPTURE_TIMEOUT_MS = 2_000;
const QUERY_CHAR_CAP = 600;

export async function buildRecallContext(params: RecallParams): Promise<string | null> {
  const timeoutMs = params.timeoutMs ?? RECALL_TIMEOUT_MS;
  const query = params.prompt.slice(0, QUERY_CHAR_CAP);
  const wantBoot = params.bootK > 0 && !params.bootedSessions.has(params.sessionKey);

  const [boot, found] = await Promise.all([
    wantBoot
      ? params.client.boot(params.agent, params.bootK, timeoutMs).catch(() => null)
      : Promise.resolve(null),
    params.client
      .search({ query, agent: params.agent, k: params.topK }, timeoutMs)
      .catch(() => null),
  ]);

  // Mark the session booted only when the boot call actually succeeded, so a
  // slow first prompt does not permanently swallow the profile block.
  if (wantBoot && boot) params.bootedSessions.add(params.sessionKey);

  return formatMemoryContext(boot?.memories ?? [], found?.hits ?? [], {
    bootCap: params.bootK,
    hitCap: params.topK,
  });
}

export type CaptureParams = {
  client: EngramClient;
  messages: unknown;
  sessionKey: string;
  cursors: Map<string, number>;
  maxItems?: number;
  timeoutMs?: number;
};

export type CaptureOutcome = {
  stored: number;
  attempted: number;
  /** Messages left for the next run (budget hit or engine unreachable). */
  remaining: number;
};

export async function captureUserTurns(params: CaptureParams): Promise<CaptureOutcome> {
  const timeoutMs = params.timeoutMs ?? CAPTURE_TIMEOUT_MS;
  const maxItems = params.maxItems ?? 6;
  const all = Array.isArray(params.messages) ? params.messages : [];

  let index = params.cursors.get(params.sessionKey) ?? 0;
  if (index > all.length) {
    // The session's message array shrank (host compaction, or a key
    // collision on a ctx-less host): indices no longer align. Recapture
    // from the start — the engine's merge rules absorb redelivery, so a
    // reset trades silent loss for safe duplicates.
    index = 0;
  }

  let stored = 0;
  let attempted = 0;

  while (index < all.length) {
    // One message = one utterance = one engine call: multi-part content is
    // joined, and the engine's own extractor splits multi-fact text. This
    // keeps the per-run budget an actual bound on work.
    const text = extractUserTexts([all[index]]).join("\n").trim();
    if (!text) {
      index++;
      continue;
    }
    if (attempted >= maxItems) {
      break; // budget spent — this message resumes next run
    }
    attempted++;
    try {
      const res = await params.client.turn({ text, speaker: "user" }, timeoutMs);
      const decision = typeof res.decision === "string" ? res.decision : "";
      if (decision === "ADD" || decision === "MULTI" || decision === "REINFORCE") {
        stored++;
      }
    } catch (err) {
      if (err instanceof EngramHttpError && err.status < 500) {
        // A 4xx is the engine's final verdict on this text — it received
        // it, rejected it, and ledgered why. Do not redeliver.
      } else {
        // 5xx or network failure: the engine decided NOTHING (a 500 means
        // remember() itself failed before any ledger entry). Keep the
        // cursor on this message so the next agent_end retries it.
        break;
      }
    }
    index++;
  }

  params.cursors.set(params.sessionKey, index);
  return { stored, attempted, remaining: all.length - index };
}
