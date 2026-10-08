#!/usr/bin/env bash
# Pins and extracts the exact TodoMVC snapshot docs/VALIDATION.md and
# tests/browser/test_client_side_persistence.py and
# tests/browser/test_inconsistent_reload_rendering.py are reproduced against.
# Safe to re-run; skips the fetch if already extracted.
set -euo pipefail
cd "$(dirname "$0")/../validation/todomvc"

if [ -d package/examples ]; then
  echo "TodoMVC examples already present at validation/todomvc/package"
  exit 0
fi

npm pack todomvc@0.1.1
tar -xzf todomvc-0.1.1.tgz
rm todomvc-0.1.1.tgz
echo "TodoMVC examples ready at validation/todomvc/package"
