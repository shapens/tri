# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:31:28Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-vun6itik`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 613.935 | 0 | 0 | 0 | 124525 |  |
| git | ok | 1357.978 | 22 | 0 | 5000 | 488303 |  |
| svn | ok | 6762.345 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 4066.974 | 26 | 0 | 18675 | 363572 |  |
| entire | ok | 6566.900 | 45 | 0 | 17275 | 175979 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
