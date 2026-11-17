# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:08:21Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-zocbju_1`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 441.555 | 6 | 0 | 4689 | 123557 |  |
| git | ok | 463.372 | 22 | 0 | 5000 | 488304 |  |
| svn | ok | 5908.711 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 2272.092 | 26 | 0 | 18675 | 363566 |  |
| entire | ok | 8016.375 | 45 | 0 | 17275 | 175971 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
