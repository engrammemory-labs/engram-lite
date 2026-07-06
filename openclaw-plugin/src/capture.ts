/**
 * Extracting user-authored text from OpenClaw message shapes.
 *
 * Deliberately thin: no memory-worthiness heuristics live here. The engine
 * decides what to keep, and its decision ledger records why anything was
 * skipped, merged, or truncated — a client-side pre-filter would just be a
 * second, invisible opinion. The only transformation is stripping channel
 * envelope headers (bracketed transport metadata carrying a channel name or
 * timestamp) so we never memorize them as if the user had said it.
 * User-authored prefixes like "[URGENT]" or "[project-x]" are preserved —
 * only brackets that look like transport metadata are touched.
 */

type ChatMessagePart = { type?: unknown; text?: unknown };
type ChatMessage = { role?: unknown; content?: unknown };

// "[WhatsApp +1555 2026-07-05 09:12] actual message" → "actual message".
// A leading bracket is treated as a transport envelope ONLY when it contains
// a channel name, a date, or a clock time. "[URGENT] ship it" passes through.
const ENVELOPE_PREFIX =
  /^\s*\[(?=[^\]\n]*(?:\d{4}-\d{2}-\d{2}|\d{1,2}[:.]\d{2}|whatsapp|telegram|slack|discord|signal|imessage|sms|email))[^\]\n]{1,120}\]\s*/i;

function textParts(content: unknown): string[] {
  if (typeof content === "string") return [content];
  if (!Array.isArray(content)) return [];
  const out: string[] = [];
  for (const part of content) {
    if (typeof part === "string") {
      out.push(part);
      continue;
    }
    if (part && typeof part === "object") {
      const p = part as ChatMessagePart;
      if (p.type === "text" && typeof p.text === "string") out.push(p.text);
    }
  }
  return out;
}

export function extractUserTexts(messages: unknown): string[] {
  if (!Array.isArray(messages)) return [];
  const out: string[] = [];
  for (const raw of messages) {
    if (!raw || typeof raw !== "object") continue;
    const message = raw as ChatMessage;
    if (message.role !== "user") continue;
    for (const text of textParts(message.content)) {
      const clean = text.replace(ENVELOPE_PREFIX, "").trim();
      if (clean) out.push(clean);
    }
  }
  return out;
}
