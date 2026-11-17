# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:27:50Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-5qlqt4h5`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1353.644 | 6 | 0 | 4669 | 124525 |  |
| git | ok | 1347.692 | 22 | 0 | 5000 | 488300 |  |
| svn | ok | 6465.207 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 4225.428 | 26 | 0 | 18675 | 363544 |  |
| entire | ok | 6952.783 | 45 | 0 | 17274 | 175968 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
