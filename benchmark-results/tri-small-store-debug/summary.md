# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:13:08Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-t0od0i6y`
- Attempts: `3`
- Fixture files: `10`
- File size: `256` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1346.581 | 6 | 0 | 1266 | 91747 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
