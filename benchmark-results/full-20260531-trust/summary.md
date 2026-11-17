# Agentic Source-Control Benchmark

- Created: `2026-05-31T23:04:58Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-ifkd4z90`
- Scenario: `full`
- Repeats: `3`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | External cmds | HTTP reqs | Embedded calls | Agent I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1977.166 | 0 | 0 | 1 | 623 | 124525 | embedded_batch |
| tri-cli | ok | 3694.404 | 1 | 0 | 0 | 623 | 124525 | cli_batch |
| tri-http | ok | 3024.926 | 1 | 7 | 0 | 31312 | 124525 | http_batch |
| git | ok | 6917.684 | 22 | 0 | 0 | 5009 | 488355 |  |
| svn | ok | 14719.714 | 20 | 0 | 0 | 51852 | 1274500 |  |
| gitbutler | ok | 11597.453 | 26 | 0 | 0 | 18693 | 363644 | Uses GitButler CLI branch stacks and operation log; patch export uses Git compatibility. |
| entire | ok | 23603.504 | 45 | 0 | 0 | 17553 | 176024 | Uses Entire's Codex hook handlers to create real local checkpoints, then commits the same file changes with Git. |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

When `Repeats` is greater than 1, table metrics are medians across successful repeats; per-repeat raw results remain in `results.json`.

`Agent I/O bytes` includes subprocess stdin/stdout/stderr, HTTP request/response/error bodies, and embedded in-process response payloads. `External cmds`, `HTTP reqs`, and `Embedded calls` split the control-plane round trips by transport so embedded Tri work no longer appears as zero interaction.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
