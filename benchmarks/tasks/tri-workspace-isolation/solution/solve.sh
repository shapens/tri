#!/usr/bin/env bash
set -euo pipefail

# Reference outline: import the repo, create two workspaces from the imported
# target, write app.txt in each workspace, read each workspace back, and create
# one changeset per workspace.
tri import "$REPO" --name app
