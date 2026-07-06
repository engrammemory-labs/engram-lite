/**
 * Loopback client for the engram sidecar (`engram serve`).
 *
 * Node's built-in fetch, bearer auth, and a hard timeout on every call:
 * a memory lookup must never be able to stall the agent. The response
 * shapes here mirror src/engram/server.py exactly — this file is the
 * only place that knows the wire format.
 */

export type ServeInfo = {
  port: number;
  token: string | null;
  db: string;
  pid: number;
  version: string;
};

export type HealthInfo = {
  ok: boolean;
  version: string;
  embedder: string;
  dim: number;
  facts: number;
  profiles: string[];
  uptime_s: number;
  requests: number;
};

export type SearchHit = {
  value: string;
  id: string;
  tags?: string[] | null;
  promotion?: string | null;
};

export type TurnResult = { decision?: string } & Record<string, unknown>;
export type LedgerDecision = { rule: string } & Record<string, unknown>;

export class EngramHttpError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "EngramHttpError";
    this.status = status;
  }
}

const DEFAULT_TIMEOUT_MS = 5_000;

export class EngramClient {
  private readonly base: string;
  private readonly token: string | null;

  constructor(port: number, token: string | null) {
    this.base = `http://127.0.0.1:${port}`;
    this.token = token;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(this.base + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const payload = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      const message =
        typeof payload["error"] === "string" ? payload["error"] : `HTTP ${res.status}`;
      throw new EngramHttpError(res.status, message);
    }
    return payload as T;
  }

  health(timeoutMs?: number): Promise<HealthInfo> {
    return this.request<HealthInfo>("GET", "/health", undefined, timeoutMs);
  }

  profile(
    params: { persona: string; agent?: string },
    timeoutMs?: number,
  ): Promise<Record<string, unknown>> {
    return this.request("POST", "/profile", params, timeoutMs);
  }

  turn(
    params: { text: string; speaker?: string; when?: string; tags?: string[] },
    timeoutMs?: number,
  ): Promise<TurnResult> {
    return this.request<TurnResult>("POST", "/turn", params, timeoutMs);
  }

  search(
    params: { query: string; agent?: string; k?: number; task_tags?: string[] },
    timeoutMs?: number,
  ): Promise<{ hits: SearchHit[] }> {
    return this.request<{ hits: SearchHit[] }>("POST", "/search", params, timeoutMs);
  }

  boot(agent: string, k: number, timeoutMs?: number): Promise<{ memories: string[] }> {
    const q = `agent=${encodeURIComponent(agent)}&k=${k}`;
    return this.request<{ memories: string[] }>("GET", `/boot?${q}`, undefined, timeoutMs);
  }

  diagnose(
    params: { kind?: string; limit?: number } = {},
    timeoutMs?: number,
  ): Promise<{ decisions: LedgerDecision[] }> {
    const parts: string[] = [];
    if (params.kind) parts.push(`kind=${encodeURIComponent(params.kind)}`);
    if (params.limit) parts.push(`limit=${params.limit}`);
    const q = parts.length ? `?${parts.join("&")}` : "";
    return this.request<{ decisions: LedgerDecision[] }>("GET", `/diagnose${q}`, undefined, timeoutMs);
  }

  shutdown(timeoutMs?: number): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>("POST", "/shutdown", {}, timeoutMs);
  }
}
