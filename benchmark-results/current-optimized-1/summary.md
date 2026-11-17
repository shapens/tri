# Agentic Source-Control Benchmark

- Created: `2026-05-29T03:37:32Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-e_tr7_mh`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 2538.890 | 6 | 24 | 56980 | 264357 |  |
| git | ok | 1346.554 | 22 | 0 | 5000 | 180588 |  |
| svn | ok | 6658.429 | 20 | 0 | 48162 | 106882 |  |
| gitbutler | ok | 3429.224 | 26 | 0 | 18675 | 363564 |  |
| entire | ok | 6978.443 | 45 | 0 | 17275 | 175193 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
