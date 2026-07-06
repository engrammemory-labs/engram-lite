/**
 * Development-time declarations for the host-provided plugin SDK.
 *
 * At runtime OpenClaw resolves `openclaw/plugin-sdk/*` itself when it loads
 * `index.ts`; these ambient modules exist only so `tsc --noEmit` can check
 * the plugin without the host installed. They intentionally describe just
 * the surface this plugin touches.
 */

declare module "openclaw/plugin-sdk/plugin-entry" {
  export type PluginLogger = {
    info(message: string): void;
    warn(message: string): void;
    error?(message: string): void;
    debug?(message: string): void;
  };

  import type { AgentToolResult } from "openclaw/plugin-sdk/agent-core";

  export type AgentToolSpec = {
    name: string;
    label: string;
    description: string;
    parameters: unknown;
    execute(
      toolCallId: string,
      params: Record<string, unknown>,
      signal?: AbortSignal,
      onUpdate?: unknown,
    ): Promise<AgentToolResult>;
  };

  export type PluginServiceSpec = {
    id: string;
    start?(): void | Promise<void>;
    stop?(): void | Promise<void>;
  };

  export type PluginHookHandler = (event: never, ctx: never) => unknown;

  export interface OpenClawPluginApi {
    pluginConfig?: Record<string, unknown>;
    logger: PluginLogger;
    resolvePath(path: string): string;
    on(
      hook: "before_prompt_build",
      handler: (
        event: { prompt?: string; messages?: unknown },
        ctx?: { sessionKey?: string; sessionId?: string },
      ) => Promise<{ prependContext: string } | undefined> | { prependContext: string } | undefined,
    ): void;
    on(
      hook: "agent_end",
      handler: (
        event: { success?: boolean; messages?: unknown },
        ctx: { sessionKey?: string; sessionId?: string },
      ) => Promise<void> | void,
    ): void;
    on(
      hook: "session_end",
      handler: (
        event: {
          sessionKey?: string;
          sessionId?: string;
          nextSessionKey?: string;
          nextSessionId?: string;
        },
        ctx: { sessionKey?: string; sessionId?: string },
      ) => void,
    ): void;
    registerTool(
      tool: AgentToolSpec | ((ctx: Record<string, unknown>) => AgentToolSpec | null),
      opts?: { name?: string; names?: string[] },
    ): void;
    registerService(service: PluginServiceSpec): void;
    registerMemoryCapability?(capability: Record<string, unknown>): void;
    registerCli?(
      register: (ctx: { program: unknown }) => void | Promise<void>,
      opts?:
        | { commands: string[] }
        | { descriptors: Array<{ name: string; description: string; hasSubcommands?: boolean }> },
    ): void;
  }

  export function definePluginEntry<T>(entry: T): T;
}

declare module "openclaw/plugin-sdk/memory-host-core" {
  export function listMemoryHostPublicArtifacts(params: unknown): Promise<unknown>;
}

declare module "openclaw/plugin-sdk/agent-core" {
  /** Matches the reference: generic over the structured details payload. */
  export type AgentToolResult<TDetails = unknown> = {
    content: Array<{ type: "text"; text: string }>;
    details?: TDetails;
  };
}
