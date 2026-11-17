Use Tri to update a workspace after its target has moved on the same path.

The expected workflow is to detect the stale same-path update as a persisted conflict, inspect the conflict object, resolve it with an explicit strategy, and confirm the workspace file content matches the selected resolution.
