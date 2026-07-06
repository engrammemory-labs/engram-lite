/**
 * Rendering memory into prompt context.
 *
 * The block is bounded on every axis (items, line length) and abstains
 * entirely — returns null — when there is nothing in the agent's lane.
 * Injecting nothing is a feature: no padding, no plausible-but-unrelated
 * filler for the model to anchor on.
 *
 * These are display bounds, not memory decisions: anything trimmed here is
 * still stored intact and reachable via memory_search, and when items are
 * held back an explicit "+N more" marker says so — the prompt never
 * silently pretends the list was complete.
 */

import type { SearchHit } from "./client.ts";

const LINE_CAP = 280;
const DEFAULT_BOOT_CAP = 8;
const DEFAULT_HIT_CAP = 6;

export type FormatCaps = {
  bootCap?: number;
  hitCap?: number;
};

function clip(line: string): string {
  const flat = line.replace(/\s+/g, " ").trim();
  return flat.length <= LINE_CAP ? flat : `${flat.slice(0, LINE_CAP - 1)}…`;
}

function dedupe(lines: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const line of lines) {
    const key = line.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(line);
  }
  return out;
}

export function formatMemoryContext(
  boot: string[],
  hits: SearchHit[],
  caps: FormatCaps = {},
): string | null {
  const bootCap = caps.bootCap ?? DEFAULT_BOOT_CAP;
  const hitCap = caps.hitCap ?? DEFAULT_HIT_CAP;

  const bootAll = dedupe(boot.map(clip));
  const bootLines = bootAll.slice(0, bootCap);
  const bootSet = new Set(bootLines.map((l) => l.toLowerCase()));
  const hitAll = dedupe(hits.map((h) => clip(h.value))).filter(
    (l) => !bootSet.has(l.toLowerCase()),
  );
  const hitLines = hitAll.slice(0, hitCap);
  const heldBack = bootAll.length - bootLines.length + (hitAll.length - hitLines.length);

  if (bootLines.length === 0 && hitLines.length === 0) return null;

  const parts: string[] = ["[engram memory]"];
  if (bootLines.length) {
    parts.push("What this agent already knows:");
    parts.push(...bootLines.map((l) => `- ${l}`));
  }
  if (hitLines.length) {
    parts.push("Relevant to the current request:");
    parts.push(...hitLines.map((l) => `- ${l}`));
  }
  if (heldBack > 0) {
    parts.push(`(+${heldBack} more stored — memory_search retrieves them in full.)`);
  }
  parts.push(
    "(memory_search finds more; memory_store saves a fact; " +
      "memory_diagnose explains every keep, merge, and drop.)",
  );
  return parts.join("\n");
}
