# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:42:19Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-knzb0nzs`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 226.806 | 0 | 0 | 0 | 124525 |  |
| git | ok | 485.266 | 22 | 0 | 5000 | 488301 |  |
| svn | ok | 6239.011 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 1474.470 | 26 | 0 | 18675 | 363567 |  |
| entire | ok | 2779.608 | 45 | 0 | 17273 | 175970 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
