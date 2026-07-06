# Engram Memory — NemoClaw sandbox image bake.
#
# NemoClaw installs OpenClaw plugins by baking them into a custom sandbox
# image (`nemoclaw onboard --from ./Dockerfile`; the parent directory is the
# build context — run ./prepare.sh first to stage the plugin source here).
#
# Everything network-dependent happens at BUILD time, outside the sandbox
# policy boundary: the engine install and the one-time embedding model
# download. At RUNTIME the memory layer needs zero egress — no network
# policy preset, no allowed endpoints, nothing for an operator to approve.

ARG SANDBOX_BASE=ghcr.io/nvidia/nemoclaw/sandbox-base:latest
FROM ${SANDBOX_BASE}

ARG PERSONA="DevOps engineer"
# Pin the exact engine release this plugin was tested against.
ARG ENGINE_SPEC="engram-lite==0.1.0"

# ── engine: own venv + embedding model baked into the image ─────────────────
# Assumption (verify once against the real base image): python3 with venv is
# present. If the base is more minimal, add the distro python3 package step
# here — see README.md.
RUN python3 -m venv /opt/engram \
 && /opt/engram/bin/pip install --no-cache-dir "${ENGINE_SPEC}"

# The model downloads ONCE, now, into the image. ENGRAM_MODEL_CACHE pins the
# cache to a stable path so the runtime never re-fetches.
ENV ENGRAM_MODEL_CACHE=/opt/engram/models
RUN /opt/engram/bin/python -c \
    "from engram.embeddings.local import LocalEmbedder; e = LocalEmbedder(); \
     print(f'model baked: {e.model_name} (dim {e.dim})')"

# ── plugin: raw TypeScript, no build step (the host's loader consumes .ts) ──
COPY plugin/ /opt/openclaw-engram/
RUN mkdir -p /sandbox/.openclaw/extensions \
 && cp -a /opt/openclaw-engram /sandbox/.openclaw/extensions/engram \
 && openclaw doctor --fix

# ── config: applied AFTER doctor --fix regenerates the base config ──────────
# pythonPath points at the baked venv, which makes the interpreter
# authoritative: the launcher ladder never falls anywhere else, and
# allowDownload stays at its default (false) — the sandbox could not
# download anyway, and never needs to.
RUN node -e ' \
  const fs = require("fs"); \
  const p = "/sandbox/.openclaw/openclaw.json"; \
  const j = JSON.parse(fs.readFileSync(p, "utf8")); \
  j.plugins = j.plugins ?? {}; \
  j.plugins.slots = { ...j.plugins.slots, memory: "engram" }; \
  j.plugins.entries = { ...j.plugins.entries, engram: { enabled: true, config: { \
    persona: process.env.PERSONA, \
    pythonPath: "/opt/engram/bin/python", \
    dbPath: "/sandbox/.engram/memory.db" \
  } } }; \
  fs.writeFileSync(p, JSON.stringify(j, null, 2)); \
  console.log("engram configured (persona: " + process.env.PERSONA + ")"); \
  ' PERSONA="${PERSONA}"
