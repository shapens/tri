# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:22:11Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-slhbe32h`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 1280.707 | 6 | 0 | 4689 | 29140 |  |
| git | ok | 1251.893 | 22 | 0 | 5000 | 488303 |  |
| svn | ok | 6215.577 | 20 | 0 | 48162 | 1274500 |  |
| gitbutler | ok | 4220.025 | 26 | 0 | 18675 | 363559 |  |
| entire | ok | 6460.788 | 45 | 0 | 17275 | 175967 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
