# Agentic Source-Control Benchmark

- Created: `2026-05-29T03:45:12Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-vuwtixac`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 2537.645 | 6 | 24 | 17273 | 220837 |  |
| git | ok | 1384.192 | 22 | 0 | 5000 | 180587 |  |
| svn | ok | 6273.237 | 20 | 0 | 48162 | 106882 |  |
| gitbutler | ok | 3577.999 | 26 | 0 | 18675 | 363569 |  |
| entire | ok | 7352.261 | 45 | 0 | 17275 | 175198 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
