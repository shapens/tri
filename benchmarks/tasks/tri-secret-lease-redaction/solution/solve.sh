#!/usr/bin/env bash
set -euo pipefail

# Reference outline: store a secret ref, request a workspace-scoped lease, pass
# it to `tri run --with-lease`, inspect redacted logs, then revoke the lease.
tri secrets request-lease "$WORKSPACE" API_TOKEN --env API_TOKEN
