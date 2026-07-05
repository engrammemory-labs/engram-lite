#!/usr/bin/env bash
# Build the standalone `engram` binary for the CURRENT platform/arch.
# Prereq:  pip install -e ".[build]"   (provides PyInstaller)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Building engram binary for $(uname -s)/$(uname -m) ..."
pyinstaller packaging/engram.spec --noconfirm --clean --distpath dist --workpath build/pyi

echo
echo "✓ binary: dist/engram"
echo "  smoke test:  ./dist/engram --help"
echo "  (per-platform: build separately on macOS arm64, macOS x86_64, Linux, Windows)"
