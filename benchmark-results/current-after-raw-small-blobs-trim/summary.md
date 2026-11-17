# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:25:10Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-28z396sy`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1547.677 | 6 | 0 | 4669 | 130106 |  |
| git | ok | 1211.145 | 22 | 0 | 5000 | 488301 |  |
| svn | ok | 6349.663 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 2921.388 | 26 | 0 | 18675 | 363563 |  |
| entire | ok | 6652.842 | 45 | 0 | 17275 | 175971 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
