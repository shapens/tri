# Agentic Source-Control Benchmark

- Created: `2026-05-31T18:19:49Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-5cie5f_i`
- Attempts: `1`
- Fixture files: `10`
- File size: `256` bytes

| Target | Status | Wall ms | External cmds | HTTP reqs | Embedded calls | Agent I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 272.197 | 0 | 0 | 1 | 586 | 11317 | embedded_batch |
| tri-cli | ok | 621.689 | 1 | 0 | 0 | 586 | 11350 | cli_batch |
| tri-http | ok | 608.492 | 1 | 4 | 0 | 15526 | 67595 | http_batch |
| git | ok | 672.543 | 11 | 0 | 0 | 1228 | 36496 |  |
| svn | ok | 2762.347 | 9 | 0 | 0 | 3326 | 191125 |  |
| gitbutler | ok | 2865.379 | 17 | 0 | 0 | 6613 | 221464 | Uses GitButler CLI branch stacks and operation log; patch export uses Git compatibility. |
| entire | ok | 4998.374 | 24 | 0 | 0 | 5743 | 42571 | Uses Entire's Codex hook handlers to create real local checkpoints, then commits the same file changes with Git. |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

`Agent I/O bytes` includes subprocess stdin/stdout/stderr, HTTP request/response/error bodies, and embedded in-process response payloads. `External cmds`, `HTTP reqs`, and `Embedded calls` split the control-plane round trips by transport so embedded Tri work no longer appears as zero interaction.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
