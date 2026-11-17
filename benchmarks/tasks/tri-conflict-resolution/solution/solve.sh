#!/usr/bin/env bash
set -euo pipefail

# Reference outline: run `tri workspace update`, inspect the persisted conflict,
# then choose an explicit `tri conflicts resolve` strategy before continuing.
tri workspace update "$WORKSPACE" --from "$TARGET" || true
