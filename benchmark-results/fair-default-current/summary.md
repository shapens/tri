# Agentic Source-Control Benchmark

- Created: `2026-05-31T18:20:13Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-ba06o504`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | External cmds | HTTP reqs | Embedded calls | Agent I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1038.063 | 0 | 0 | 1 | 623 | 124525 | embedded_batch |
| tri-cli | ok | 1527.761 | 1 | 0 | 0 | 623 | 124525 | cli_batch |
| tri-http | ok | 1196.312 | 1 | 4 | 0 | 31124 | 124525 | http_batch |
| git | ok | 1875.326 | 22 | 0 | 0 | 5000 | 488304 |  |
| svn | ok | 7708.776 | 20 | 0 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 1102.027 | 26 | 0 | 0 | 18675 | 363540 | Uses GitButler CLI branch stacks and operation log; patch export uses Git compatibility. |
| entire | ok | 2742.587 | 45 | 0 | 0 | 17275 | 175970 | Uses Entire's Codex hook handlers to create real local checkpoints, then commits the same file changes with Git. |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

`Agent I/O bytes` includes subprocess stdin/stdout/stderr, HTTP request/response/error bodies, and embedded in-process response payloads. `External cmds`, `HTTP reqs`, and `Embedded calls` split the control-plane round trips by transport so embedded Tri work no longer appears as zero interaction.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
