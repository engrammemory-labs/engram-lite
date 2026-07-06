/**
 * Sidecar supervision for `engram serve`.
 *
 * The plugin owns the daemon's whole lifecycle: pick a launcher, spawn it,
 * read the startup contract from the first stdout line, keep both pipes
 * drained (a stalled pipe must never wedge the daemon), restart on crashes
 * with capped backoff, and shut down cleanly so the store checkpoints.
 *
 * Launcher resolution:
 *   1. explicit interpreter (config.pythonPath or ENGRAM_PYTHON) — authoritative:
 *      if the user named an interpreter and it is missing, that is an error,
 *      not a reason to silently run something else;
 *   2. `engram` on PATH (pip/pipx/uv tool installs);
 *   3. only when `allowDownload` is explicitly enabled: `uvx` / `pipx run`
 *      against a version-pinned engram-lite. Downloading and executing a
 *      package is never something a memory plugin does silently — by default
 *      the ladder is offline-only and the zero-egress property holds
 *      unconditionally.
 * Auto-discovery cascades only on ENOENT. Any real failure stops the ladder
 * and reports the command tried plus the daemon's stderr tail.
 *
 * Signals are delivered to the whole process group (spawn is detached), so
 * a hard kill can never orphan the real daemon behind a launcher wrapper
 * like uvx or pipx.
 */

import { spawn, type ChildProcess } from "node:child_process";

import { EngramClient, type ServeInfo } from "./client.ts";

export type Logger = {
  info(message: string): void;
  warn(message: string): void;
};

export type SidecarOptions = {
  dbPath: string;
  agent?: string;
  pythonPath?: string;
  /** Opt-in: allow uvx/pipx to fetch the (version-pinned) engine. Default false. */
  allowDownload?: boolean;
  startupTimeoutMs?: number;
  env?: Record<string, string | undefined>;
  logger?: Logger;
  signal?: AbortSignal;
};

export type LaunchCandidate = {
  kind: "python" | "path" | "uvx" | "pipx";
  cmd: string;
  args: string[];
  explicit: boolean;
};

export type SidecarHandle = {
  info: ServeInfo;
  client: EngramClient;
  proc: ChildProcess;
  stderrTail(): string[];
  exited(): boolean;
  stop(): Promise<void>;
};

/**
 * The engine version the download rungs are pinned to. A downloaded engine
 * is exactly the release this plugin was tested against — never "latest".
 */
export const ENGINE_PIN = "engram-lite==0.2.0";

// First-run `engram serve` may download the embedding model before it can
// print the startup contract, so even the direct rungs get a generous
// budget; `startupTimeoutMs` in config overrides both.
const DIRECT_STARTUP_TIMEOUT_MS = 120_000;
const COLD_STARTUP_TIMEOUT_MS = 300_000; // uvx/pipx: package download + model download
const STDERR_TAIL_LINES = 50;

/** Bearer tokens must never ride along into error messages or logs. */
function redactTokens(text: string): string {
  return text.replace(/"token"\s*:\s*"[^"]*"/g, '"token":"[redacted]"');
}

/** Signal the whole process group; fall back to the direct child. */
function killTree(proc: ChildProcess, signal: NodeJS.Signals): void {
  const pid = proc.pid;
  if (pid === undefined) return;
  try {
    process.kill(-pid, signal); // detached spawn → pgid === pid
  } catch {
    try {
      proc.kill(signal);
    } catch {
      // already gone
    }
  }
}

export function launchCandidates(opts: SidecarOptions): LaunchCandidate[] {
  const serveArgs = [
    "serve",
    "--db",
    opts.dbPath,
    "--port",
    "0",
    "--state",
    `${opts.dbPath}.serve.json`,
    ...(opts.agent ? ["--agent", opts.agent] : []),
  ];
  const explicitPython =
    opts.pythonPath ?? opts.env?.["ENGRAM_PYTHON"] ?? process.env["ENGRAM_PYTHON"];
  if (explicitPython) {
    return [
      {
        kind: "python",
        cmd: explicitPython,
        args: ["-m", "engram.cli.main", ...serveArgs],
        explicit: true,
      },
    ];
  }
  const candidates: LaunchCandidate[] = [
    { kind: "path", cmd: "engram", args: serveArgs, explicit: false },
  ];
  if (opts.allowDownload === true) {
    candidates.push(
      {
        kind: "uvx",
        cmd: "uvx",
        args: ["--from", ENGINE_PIN, "engram", ...serveArgs],
        explicit: false,
      },
      {
        kind: "pipx",
        cmd: "pipx",
        args: ["run", "--spec", ENGINE_PIN, "engram", ...serveArgs],
        explicit: false,
      },
    );
  }
  return candidates;
}

type SpawnFailure = {
  enoent: boolean;
  message: string;
};

function spawnOne(
  candidate: LaunchCandidate,
  opts: SidecarOptions,
): Promise<{ proc: ChildProcess; info: ServeInfo; stderrRing: string[] }> {
  return new Promise((resolve, reject) => {
    const timeoutMs =
      opts.startupTimeoutMs ??
      (candidate.kind === "uvx" || candidate.kind === "pipx"
        ? COLD_STARTUP_TIMEOUT_MS
        : DIRECT_STARTUP_TIMEOUT_MS);

    if (opts.signal?.aborted) {
      reject(Object.assign(new Error("sidecar start aborted"), { enoent: false }));
      return;
    }

    const proc = spawn(candidate.cmd, candidate.args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, ...opts.env },
      detached: true, // own process group → hard kills reach wrapper AND daemon
    });

    let settled = false;
    let stdoutBuf = "";
    const stderrRing: string[] = [];

    const onAbort = () => {
      fail({ enoent: false, message: "sidecar start aborted" });
    };
    opts.signal?.addEventListener("abort", onAbort, { once: true });

    const fail = (failure: SpawnFailure) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
      if (proc.exitCode === null && !proc.killed) {
        killTree(proc, "SIGKILL");
      }
      reject(Object.assign(new Error(failure.message), { enoent: failure.enoent }));
    };

    const timer = setTimeout(() => {
      fail({
        enoent: false,
        message:
          `\`${candidate.cmd} ${candidate.args.join(" ")}\` did not print its startup ` +
          `contract within ${timeoutMs}ms (a first run may be downloading the embedding ` +
          `model; raise \`startupTimeoutMs\` in the plugin config if your network needs longer)` +
          (stderrRing.length ? `; stderr tail:\n${stderrRing.join("\n")}` : ""),
      });
    }, timeoutMs);
    timer.unref?.();

    proc.once("error", (err: NodeJS.ErrnoException) => {
      fail({
        enoent: err.code === "ENOENT",
        message: `failed to spawn \`${candidate.cmd}\`: ${err.message}`,
      });
    });

    proc.stderr?.on("data", (chunk: Buffer) => {
      for (const line of chunk.toString().split("\n")) {
        if (!line.trim()) continue;
        stderrRing.push(line);
        if (stderrRing.length > STDERR_TAIL_LINES) stderrRing.shift();
      }
    });

    proc.stdout?.on("data", (chunk: Buffer) => {
      if (settled) return; // post-contract stdout: drained and discarded
      stdoutBuf += chunk.toString();
      const nl = stdoutBuf.indexOf("\n");
      if (nl < 0) return;
      const first = stdoutBuf.slice(0, nl);
      try {
        const parsed = JSON.parse(first) as { engram_serve?: ServeInfo };
        if (!parsed.engram_serve) throw new Error("missing engram_serve key");
        settled = true;
        clearTimeout(timer);
        opts.signal?.removeEventListener("abort", onAbort);
        resolve({ proc, info: parsed.engram_serve, stderrRing });
      } catch {
        fail({
          enoent: false,
          message:
            `\`${candidate.cmd}\` produced unexpected startup output: ` +
            `${redactTokens(first).slice(0, 200)}`,
        });
      }
    });

    proc.once("exit", (code) => {
      fail({
        enoent: false,
        message:
          `\`${candidate.cmd} ${candidate.args.join(" ")}\` exited with code ${code} ` +
          `before printing its startup contract` +
          (stderrRing.length ? `; stderr tail:\n${stderrRing.join("\n")}` : ""),
      });
    });
  });
}

export async function startSidecar(opts: SidecarOptions): Promise<SidecarHandle> {
  const candidates = launchCandidates(opts);
  const attempts: string[] = [];

  for (const candidate of candidates) {
    if (opts.signal?.aborted) {
      throw new Error("engram sidecar start aborted");
    }
    let spawned: { proc: ChildProcess; info: ServeInfo; stderrRing: string[] };
    try {
      spawned = await spawnOne(candidate, opts);
    } catch (err) {
      const failure = err as Error & { enoent?: boolean };
      attempts.push(`${candidate.cmd}: ${failure.message}`);
      if (failure.enoent && !candidate.explicit) {
        continue; // launcher not installed — try the next rung
      }
      const kindNote = candidate.explicit
        ? "the configured interpreter is authoritative; not falling back"
        : "stopping the launcher ladder on a real failure";
      throw new Error(
        `engram sidecar failed to start (${kindNote}).\nTried:\n  ${attempts.join("\n  ")}`,
      );
    }

    const { proc, info, stderrRing } = spawned;
    const client = new EngramClient(info.port, info.token);
    let hasExited = false;
    let stopRequested = false;
    proc.once("exit", () => {
      hasExited = true;
    });

    // The contract line is printed after the socket binds, so one health
    // probe (with a couple of retries for scheduler jitter) confirms the
    // daemon end to end before anything else talks to it.
    let healthy = false;
    for (let attempt = 0; attempt < 3 && !healthy && !opts.signal?.aborted; attempt++) {
      try {
        const health = await client.health(2_000);
        healthy = health.ok === true;
      } catch {
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    if (!healthy) {
      killTree(proc, "SIGKILL");
      if (opts.signal?.aborted) {
        throw new Error("engram sidecar start aborted");
      }
      throw new Error(
        `engram sidecar started (pid ${info.pid}, port ${info.port}) but never became healthy` +
          (stderrRing.length ? `; stderr tail:\n${stderrRing.join("\n")}` : ""),
      );
    }

    const waitForExit = (ms: number): Promise<boolean> =>
      new Promise((resolveExit) => {
        if (hasExited) return resolveExit(true);
        const t = setTimeout(() => resolveExit(hasExited), ms);
        t.unref?.();
        proc.once("exit", () => {
          clearTimeout(t);
          resolveExit(true);
        });
      });

    return {
      info,
      client,
      proc,
      stderrTail: () => [...stderrRing],
      exited: () => hasExited,
      stop: async () => {
        if (stopRequested || hasExited) return;
        stopRequested = true;
        try {
          await client.shutdown(2_000); // clean path: WAL checkpoint before exit
        } catch {
          // fall through to signals
        }
        if (await waitForExit(2_500)) return;
        killTree(proc, "SIGTERM");
        if (await waitForExit(2_000)) return;
        killTree(proc, "SIGKILL");
        await waitForExit(1_000);
      },
    };
  }

  const downloadHint =
    opts.allowDownload === true
      ? ""
      : "\n(The plugin never downloads the engine on its own. Set `allowDownload: true` " +
        "in the plugin config to let uvx/pipx fetch the pinned release, or install it yourself.)";
  throw new Error(
    "engram sidecar could not be started: no launcher found.\nTried:\n  " +
      attempts.join("\n  ") +
      "\nInstall engram-lite (`pip install engram-lite`, uv, or pipx) or set " +
      "`pythonPath` in the plugin config to an interpreter that has it." +
      downloadHint,
  );
}

const RESTART_BACKOFF_MS = [1_000, 5_000, 25_000];
const RESTART_WINDOW_MS = 10 * 60_000;

/**
 * Keeps one sidecar alive: failed initial starts and post-start crashes go
 * through the same capped retry budget (3 attempts per 10-minute window),
 * so a transient boot-time failure is recoverable, not permanent. Hooks
 * read `client()` — null while the daemon is down or starting — so memory
 * degrades to "unavailable" and the agent keeps working; nothing in the hot
 * path ever waits on a spawn, and `stop()` aborts an in-flight start
 * instead of waiting it out.
 */
export class SidecarManager {
  private readonly opts: SidecarOptions;
  private readonly logger: Logger | undefined;
  private readonly onUp: ((handle: SidecarHandle) => void | Promise<void>) | undefined;
  private handle: SidecarHandle | null = null;
  private starting: Promise<SidecarHandle> | null = null;
  private abort: AbortController | null = null;
  private stopped = false;
  private failureTimes: number[] = [];

  constructor(opts: SidecarOptions & { onUp?: (handle: SidecarHandle) => void | Promise<void> }) {
    const { onUp, ...rest } = opts;
    this.opts = rest;
    this.logger = opts.logger;
    this.onUp = onUp;
  }

  /** Spawn (or return the in-flight spawn). Never called from hooks. */
  start(): Promise<SidecarHandle> {
    if (this.stopped) return Promise.reject(new Error("sidecar manager is stopped"));
    if (this.handle && !this.handle.exited()) return Promise.resolve(this.handle);
    if (!this.starting) {
      this.abort = new AbortController();
      this.starting = startSidecar({ ...this.opts, signal: this.abort.signal })
        .then(async (handle) => {
          this.handle = handle;
          this.starting = null;
          this.watch(handle);
          this.logger?.info(
            `engram: sidecar up (pid ${handle.info.pid}, port ${handle.info.port}, ` +
              `db ${handle.info.db}, v${handle.info.version})`,
          );
          try {
            await this.onUp?.(handle);
          } catch (err) {
            this.logger?.warn(`engram: post-start hook failed: ${String(err)}`);
          }
          return handle;
        })
        .catch((err) => {
          this.starting = null;
          this.scheduleRetry(`start failed: ${String(err instanceof Error ? err.message : err)}`);
          throw err;
        });
    }
    return this.starting;
  }

  /** The live client, or null when memory is unavailable. Non-blocking. */
  client(): EngramClient | null {
    if (this.stopped || !this.handle || this.handle.exited()) return null;
    return this.handle.client;
  }

  current(): SidecarHandle | null {
    return this.handle && !this.handle.exited() ? this.handle : null;
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.abort?.abort(); // cancel an in-flight start instead of waiting it out
    const inFlight = this.starting;
    if (inFlight) {
      try {
        await inFlight;
      } catch {
        // an aborted/failed start has nothing to stop
      }
    }
    await this.handle?.stop();
    this.handle = null;
  }

  /** One retry budget for both failure modes: failed starts and crashes. */
  private scheduleRetry(context: string): void {
    if (this.stopped) return;
    const now = Date.now();
    this.failureTimes = this.failureTimes.filter((t) => now - t < RESTART_WINDOW_MS);
    this.failureTimes.push(now);
    const nth = this.failureTimes.length;
    if (nth > RESTART_BACKOFF_MS.length) {
      this.logger?.warn(
        `engram: sidecar failed ${nth} times in 10 minutes (${context}); ` +
          "giving up — memory is unavailable until the next gateway restart.",
      );
      return;
    }
    const delay = RESTART_BACKOFF_MS[nth - 1] ?? 25_000;
    this.logger?.warn(`engram: ${context}; retry ${nth}/${RESTART_BACKOFF_MS.length} in ${delay}ms`);
    const timer = setTimeout(() => {
      if (this.stopped) return;
      this.start().catch(() => {
        // scheduleRetry already queued the next attempt (or gave up)
      });
    }, delay);
    timer.unref?.();
  }

  private watch(handle: SidecarHandle): void {
    handle.proc.once("exit", (code) => {
      if (this.stopped) return;
      const tail = handle.stderrTail();
      this.scheduleRetry(
        `sidecar exited unexpectedly (code ${code})` +
          (tail.length ? `; stderr tail:\n${tail.join("\n")}` : ""),
      );
    });
  }
}
