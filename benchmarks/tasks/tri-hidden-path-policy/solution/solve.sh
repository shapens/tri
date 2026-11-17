#!/usr/bin/env bash
set -euo pipefail

# Reference outline: create the workspace with --hide-path 'private/**', use
# Tri files list/search/read rather than raw filesystem reads, and only export
# public changes.
tri workspace create app --from "$TARGET" --hide-path 'private/**'
