/** Hook logic against the real daemon: recall injection, capture cursors, fail-open. */

import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { EngramClient, EngramHttpError } from "../src/client.ts";
import { buildRecallContext, captureUserTurns } from "../src/hooks.ts";
import { extractUserTexts } from "../src/capture.ts";
import { formatMemoryContext } from "../src/format.ts";
import { launchDaemon, type DaemonFixture } from "./helpers.ts";

let fixture: DaemonFixture;

before(async () => {
  fixture = await launchDaemon();
  await fixture.handle.client.profile({ persona: "DevOps engineer", agent: "openclaw" });
});

after(async () => {
  await fixture.cleanup();
});

test("capture: user turns land, envelope headers are stripped, cursor prevents replay", async () => {
  const client = fixture.handle.client;
  const cursors = new Map<string, number>();
  const messages = [
    { role: "user", content: "The canary threshold is 2 percent error rate" },
    { role: "assistant", content: "Noted — I'll watch the canary." },
    {
      role: "user",
      content: [
        { type: "text", text: "[WhatsApp +1555 2026-07-05 09:12] The deploy freeze is every Friday" },
      ],
    },
  ];

  const first = await captureUserTurns({
    client,
    messages,
    sessionKey: "s1",
    cursors,
  });
  assert.ok(first.stored >= 2, `expected >=2 stored, got ${first.stored}`);
  assert.equal(first.attempted, 2); // assistant message is not captured

  // the envelope header never reaches the store
  const { hits } = await client.search({ query: "when is the deploy freeze?", agent: "openclaw" });
  assert.ok(hits.some((h) => h.value.includes("Friday")));
  assert.ok(hits.every((h) => !h.value.includes("[WhatsApp")));

  // same messages again: cursor makes replay a no-op
  const second = await captureUserTurns({ client, messages, sessionKey: "s1", cursors });
  assert.equal(second.attempted, 0);

  // a NEW message after the cursor is picked up
  const extended = [...messages, { role: "user", content: "Rollbacks go through ArgoCD only" }];
  const third = await captureUserTurns({ client, messages: extended, sessionKey: "s1", cursors });
  assert.equal(third.attempted, 1);
});

test("recall: boot block once per session, then search-only", async () => {
  const client = fixture.handle.client;
  const booted = new Set<string>();

  const first = await buildRecallContext({
    client,
    agent: "openclaw",
    prompt: "what is the canary threshold?",
    topK: 4,
    bootK: 6,
    sessionKey: "s2",
    bootedSessions: booted,
  });
  assert.ok(first, "expected a memory context on first prompt");
  assert.ok(first.includes("[engram memory]"));
  assert.ok(first.includes("2 percent"));
  assert.ok(booted.has("s2"));

  const second = await buildRecallContext({
    client,
    agent: "openclaw",
    prompt: "what is the canary threshold?",
    topK: 4,
    bootK: 6,
    sessionKey: "s2",
    bootedSessions: booted,
  });
  assert.ok(second, "search hits should still inject");
  assert.ok(!second.includes("What this agent already knows:"), "boot block must not repeat");
});

test("recall: abstains (null) for an agent whose lane is empty", async () => {
  // Note: the CAPTURING agent always sees its own memories (provenance
  // channel), even on unrelated queries — that is by design. Abstention is
  // what a DIFFERENT agent gets: an empty lane injects nothing, ever.
  const client = fixture.handle.client;
  await client.profile({ persona: "HR assistant", agent: "hr-bot" });
  const context = await buildRecallContext({
    client,
    agent: "hr-bot",
    prompt: "what is our leave policy?",
    topK: 4,
    bootK: 6,
    sessionKey: "s3",
    bootedSessions: new Set<string>(),
  });
  assert.equal(context, null); // no filler, no cross-domain leak — abstention
});

test("recall and capture fail open when the daemon is unreachable", async () => {
  // A dead port is the honest failure mode (a 1ms timeout race loses to a
  // warm loopback daemon that answers in microseconds). Hooks must yield
  // null / zero counts — never throw into the agent loop.
  const dead = new EngramClient(1, "irrelevant-token");
  const context = await buildRecallContext({
    client: dead,
    agent: "openclaw",
    prompt: "what is the canary threshold?",
    topK: 4,
    bootK: 6,
    sessionKey: "s4",
    bootedSessions: new Set<string>(),
    timeoutMs: 1_000,
  });
  assert.equal(context, null);

  const cursors = new Map<string, number>();
  const outcome = await captureUserTurns({
    client: dead,
    messages: [{ role: "user", content: "a fact that cannot land" }],
    sessionKey: "s4",
    cursors,
    timeoutMs: 1_000,
  });
  assert.equal(outcome.stored, 0);
  assert.equal(outcome.attempted, 1);
  // engine unreachable → cursor did NOT advance: the message is retried
  // next run instead of being silently consumed
  assert.equal(cursors.get("s4"), 0);
  assert.equal(outcome.remaining, 1);
});

test("capture: per-run budget defers overflow instead of dropping it", async () => {
  const client = fixture.handle.client;
  const cursors = new Map<string, number>();
  const messages = Array.from({ length: 5 }, (_, i) => ({
    role: "user",
    content: `Overflow fact ${i}: service tier ${i} owns queue partition ${i}`,
  }));

  const first = await captureUserTurns({
    client,
    messages,
    sessionKey: "s5",
    cursors,
    maxItems: 2,
  });
  assert.equal(first.attempted, 2);
  assert.equal(first.remaining, 3); // deferred, NOT dropped

  const second = await captureUserTurns({
    client,
    messages,
    sessionKey: "s5",
    cursors,
    maxItems: 2,
  });
  assert.equal(second.attempted, 2);
  assert.equal(second.remaining, 1);

  const third = await captureUserTurns({ client, messages, sessionKey: "s5", cursors });
  assert.equal(third.attempted, 1);
  assert.equal(third.remaining, 0);
  assert.equal(cursors.get("s5"), 5); // every message eventually reached the engine
});

test("capture: user-authored bracket prefixes are preserved, transport envelopes stripped", () => {
  assert.deepEqual(
    extractUserTexts([
      { role: "user", content: "[URGENT] the prod deploy freeze starts Friday" },
      { role: "user", content: "[project-atlas] budget is 40k" },
      { role: "user", content: "[WhatsApp +1555 2026-07-05 09:12] rotation is monthly" },
      { role: "user", content: "[Slack #ops 09:15] canary at 2 percent" },
    ]),
    [
      "[URGENT] the prod deploy freeze starts Friday", // user-authored: untouched
      "[project-atlas] budget is 40k", // user-authored: untouched
      "rotation is monthly", // transport envelope: stripped
      "canary at 2 percent", // transport envelope: stripped
    ],
  );
});

test("extractUserTexts handles strings, parts arrays, and junk shapes", () => {
  assert.deepEqual(
    extractUserTexts([
      { role: "user", content: "plain" },
      { role: "user", content: [{ type: "text", text: "in parts" }, { type: "image" }] },
      { role: "assistant", content: "not mine" },
      { role: "user", content: 42 },
      null,
      "not even an object",
    ]),
    ["plain", "in parts"],
  );
  assert.deepEqual(extractUserTexts("not an array"), []);
});

test("formatMemoryContext bounds, dedupes, and abstains", () => {
  assert.equal(formatMemoryContext([], []), null);

  const long = "x".repeat(500);
  const context = formatMemoryContext(
    [long, "fact A", "fact a"], // near-dupes collapse case-insensitively
    [
      { value: "fact A", id: "1" }, // already in boot → dropped
      { value: "fresh hit", id: "2", promotion: "lane" },
    ],
  );
  assert.ok(context);
  const lines = context.split("\n");
  assert.ok(lines.every((l) => l.length <= 300));
  assert.equal(lines.filter((l) => l.toLowerCase().includes("fact a")).length, 1);
  assert.ok(context.includes("fresh hit"));
});

test("capture: cursor resets when the message array shrinks (compaction/key collision)", async () => {
  const client = fixture.handle.client;
  const cursors = new Map<string, number>();
  cursors.set("s6", 40); // stale cursor from a longer, pre-compaction history
  const outcome = await captureUserTurns({
    client,
    messages: [
      { role: "user", content: "Post-compaction fact: the audit runs quarterly" },
      { role: "assistant", content: "ok" },
    ],
    sessionKey: "s6",
    cursors,
  });
  assert.equal(outcome.attempted, 1); // NOT silently skipped
  assert.equal(outcome.remaining, 0); // never negative
  assert.equal(cursors.get("s6"), 2);
});

test("capture: 4xx is a final engine verdict, 5xx/network resumes", async () => {
  const turns: string[] = [];
  const stub = (status: number | "network") =>
    ({
      turn: async (p: { text: string }) => {
        turns.push(p.text);
        if (status === "network") throw new TypeError("fetch failed");
        throw new EngramHttpError(status, `HTTP ${status}`);
      },
    }) as unknown as EngramClient;
  const messages = [
    { role: "user", content: "fact one" },
    { role: "user", content: "fact two" },
  ];

  // 400: engine saw and rejected both — cursor advances past both, no retry
  const c400 = new Map<string, number>();
  const r400 = await captureUserTurns({ client: stub(400), messages, sessionKey: "x", cursors: c400 });
  assert.equal(r400.attempted, 2);
  assert.equal(c400.get("x"), 2);

  // 500: engine decided NOTHING — halt on the first message, retry next run
  const c500 = new Map<string, number>();
  const r500 = await captureUserTurns({ client: stub(500), messages, sessionKey: "x", cursors: c500 });
  assert.equal(r500.attempted, 1);
  assert.equal(c500.get("x"), 0); // cursor did not move
  assert.equal(r500.remaining, 2);

  // network error: same halt-and-resume semantics
  const cNet = new Map<string, number>();
  const rNet = await captureUserTurns({ client: stub("network"), messages, sessionKey: "x", cursors: cNet });
  assert.equal(rNet.attempted, 1);
  assert.equal(cNet.get("x"), 0);
});

test("capture: multi-part message is ONE utterance, one engine call, one budget unit", async () => {
  const client = fixture.handle.client;
  const cursors = new Map<string, number>();
  const outcome = await captureUserTurns({
    client,
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: "The billing cutoff is the 25th" },
          { type: "text", text: "and invoices go out on the 28th" },
        ],
      },
    ],
    sessionKey: "s7",
    cursors,
    maxItems: 1, // a many-part message cannot blow past the budget
  });
  assert.equal(outcome.attempted, 1);
  assert.equal(outcome.remaining, 0);
  const { hits } = await client.search({ query: "when is the billing cutoff?", agent: "openclaw" });
  assert.ok(hits.some((h) => h.value.includes("25th")));
});
