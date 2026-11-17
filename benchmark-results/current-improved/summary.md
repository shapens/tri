# Agentic Source-Control Benchmark

- Created: `2026-05-29T03:28:33Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-494tpvdv`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 5619.227 | 6 | 25 | 72178 | 362726 |  |
| git | ok | 2701.171 | 22 | 0 | 5000 | 180590 |  |
| svn | ok | 7350.983 | 20 | 0 | 48162 | 106882 |  |
| gitbutler | ok | 4067.514 | 26 | 0 | 18675 | 363571 |  |
| entire | ok | 7337.067 | 45 | 0 | 17273 | 175189 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
