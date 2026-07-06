#!/usr/bin/env bash
# Build the proof image (network ON — the bake), then run it with the
# network physically removed (--network none) and let proof.py verify the
# full memory loop plus the sealed cage.
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd ../../.. && pwd)"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/engine-src"
cp -a "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/README.md" "$REPO_ROOT/LICENSE" \
      "$REPO_ROOT/src" "$STAGE/engine-src/"
cp -a proof.py Dockerfile "$STAGE/"

echo "── build (network ON: pip + model bake) ──"
docker build -q -t engram-zero-egress-proof "$STAGE"

echo "── run (network NONE) ──"
docker run --rm --network none engram-zero-egress-proof
