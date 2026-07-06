#!/usr/bin/env bash
# Stage the plugin source into this directory as the Docker build context
# expects it (nemoclaw onboard --from uses the Dockerfile's parent dir).
set -euo pipefail
cd "$(dirname "$0")"

rm -rf plugin
mkdir -p plugin
# ship exactly what the npm package ships — nothing dev-only
for item in index.ts src types openclaw.plugin.json package.json README.md LICENSE; do
  cp -a "../${item}" plugin/
done

echo "staged: $(find plugin -type f | wc -l | tr -d ' ') files under $(pwd)/plugin"
echo "next:   nemoclaw onboard --from $(pwd)/Dockerfile"
