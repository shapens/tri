#!/usr/bin/env bash
set -euo pipefail

# Reference outline: create a changeset, observe export policy denial, run a
# passing check, create a fresh changeset, approve review, then export patch.
tri policy evaluate export "$CHANGESET" || true
