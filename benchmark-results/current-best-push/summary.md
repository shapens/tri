# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:02:35Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-ylup3kjx`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 843.804 | 6 | 0 | 4689 | 141989 |  |
| git | ok | 498.633 | 22 | 0 | 5000 | 180589 |  |
| svn | ok | 5745.301 | 20 | 0 | 48162 | 106882 |  |
| gitbutler | ok | 1429.710 | 26 | 0 | 18675 | 363544 |  |
| entire | ok | 2996.766 | 45 | 0 | 17275 | 175196 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
