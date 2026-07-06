/**
 * Test fixtures: every test here runs against the REAL Python daemon.
 *
 * Point ENGRAM_TEST_PYTHON at an interpreter with engram-lite installed
 * (defaults to a local dev path). Tests use the hash embedder so no model
 * download or GPU is involved.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

import { startSidecar, type SidecarHandle, type SidecarOptions } from "../src/sidecar.ts";

export function testPython(): string {
  // point ENGRAM_TEST_PYTHON at an interpreter with engram-lite installed;
  // the fallback assumes `python3` on PATH can import it
  return process.env["ENGRAM_TEST_PYTHON"] ?? "python3";
}

export type DaemonFixture = {
  handle: SidecarHandle;
  dbPath: string;
  cleanup(): Promise<void>;
};

export async function launchDaemon(
  overrides: Partial<SidecarOptions> = {},
): Promise<DaemonFixture> {
  const dir = mkdtempSync(join(tmpdir(), "engram-openclaw-test-"));
  const dbPath = join(dir, "memory.db");
  const handle = await startSidecar({
    dbPath,
    agent: "openclaw",
    pythonPath: testPython(),
    env: { ENGRAM_EMBEDDER: "hash" },
    ...overrides,
  });
  return {
    handle,
    dbPath,
    cleanup: async () => {
      await handle.stop();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}
