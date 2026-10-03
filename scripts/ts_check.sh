#!/usr/bin/env bash
# Builds and tests every TypeScript package in the repo: npm ci, build, test.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
for pkg in extensions/relay-bridge plugins/opencode; do
  if [ -f "$root/$pkg/package.json" ]; then
    echo "== $pkg"
    (cd "$root/$pkg" && npm ci --no-audit --no-fund --silent && npm run --silent build && npm test --silent)
  fi
done
