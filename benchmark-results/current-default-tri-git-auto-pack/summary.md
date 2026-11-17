# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:27:11Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-yzf2pgtq`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1257.109 | 6 | 0 | 4669 | 124525 |  |
| git | ok | 1317.860 | 22 | 0 | 5000 | 488300 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
