# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:17:07Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-butfg9tt`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1342.882 | 6 | 0 | 4689 | 29116 |  |
| git | ok | 1556.178 | 22 | 0 | 5000 | 488302 |  |
| svn | ok | 6813.054 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 3395.169 | 26 | 0 | 18675 | 363562 |  |
| entire | ok | 7022.675 | 45 | 0 | 17275 | 175974 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
