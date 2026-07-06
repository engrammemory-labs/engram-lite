# Engram Memory in NemoClaw

NemoClaw runs OpenClaw inside a sandbox with a deny-by-default network
policy: every outbound endpoint must be explicitly allowed, and anything
else prompts the operator. Memory backends that call a cloud API need
egress holes punched for them.

Engram needs **no network policy entries at all**. The engine, the
embedding model, and the plugin are baked into the sandbox image at build
time; at runtime the memory layer speaks only to `127.0.0.1`.

## Bake

```bash
./prepare.sh                      # stages the plugin source into ./plugin/
nemoclaw onboard --from ./Dockerfile
# optional: --build-arg PERSONA="security engineer"
```

The Dockerfile:
1. installs the pinned engine (`engram-lite==0.1.0`) into `/opt/engram`,
2. downloads the embedding model once, into the image
   (`ENGRAM_MODEL_CACHE=/opt/engram/models`),
3. copies the plugin to `/sandbox/.openclaw/extensions/engram`
   (raw TypeScript — no build step) and runs `openclaw doctor --fix`,
4. writes the plugin config after doctor: memory slot, persona,
   `pythonPath=/opt/engram/bin/python` (authoritative — the launcher ladder
   never looks anywhere else; `allowDownload` stays false).

## Network policy

Nothing to add. Do not create a preset for engram; its absence is the point.

## Prove it

`proof/run.sh` builds a minimal image the same way (engine + model baked at
build time) and then runs the complete memory loop — profile derivation,
capture, semantic search with the real embedding model, lane snapshot,
decision ledger, auth wall, clean shutdown — in a container started with
`--network none`. The proof first verifies the cage is sealed (no TCP, no
DNS) so a pass cannot be faked by a leaky environment.

```bash
proof/run.sh
```

## Assumptions to verify on the real sandbox base image

- `python3` with `venv` is available in `ghcr.io/nvidia/nemoclaw/sandbox-base`.
  If the base is more minimal, add the distro python3 install step before the
  venv line.
- `openclaw doctor --fix` regenerates config at `/sandbox/.openclaw/openclaw.json`
  (per the NemoClaw plugin-install docs); the config-merge step runs after it.
- Hook parity inside the sandbox (before_prompt_build / agent_end) — same
  verification as the plain-OpenClaw live E2E.
