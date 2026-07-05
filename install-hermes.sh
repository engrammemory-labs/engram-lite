#!/usr/bin/env bash
# Install engram memory into a local Hermes — from this folder, no network.
#
#   ./install-hermes.sh
#
# Then:  hermes memory setup   (one question)  →  hermes chat
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV_PY="$HERMES_HOME/hermes-agent/venv/bin/python"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$VENV_PY" ]; then
  echo "✗ Hermes not found at $HERMES_HOME (set HERMES_HOME if it lives elsewhere)"
  exit 1
fi

echo "→ installing the engram engine into Hermes's environment…"
"$VENV_PY" -m pip install --quiet "$HERE"

echo "→ installing the memory plugin…"
mkdir -p "$HERMES_HOME/plugins"
rm -rf "$HERMES_HOME/plugins/engram"
cp -R "$HERE/hermes-plugin/engram" "$HERMES_HOME/plugins/engram"

echo "✓ done. Next:"
echo "    hermes memory setup   # one question: what is this agent?"
echo "    hermes chat"
