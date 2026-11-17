# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:18:26Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-jfj6hgr8`
- Attempts: `3`
- Fixture files: `10`
- File size: `256` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 999.602 | 6 | 0 | 1266 | 13456 |  |
| git | ok | 1330.543 | 22 | 0 | 1578 | 47134 |  |
| svn | ok | 6653.749 | 20 | 0 | 6672 | 452050 |  |
| gitbutler | ok | 2876.655 | 26 | 0 | 15253 | 236149 |  |
| entire | ok | 6095.978 | 45 | 0 | 13853 | 51579 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
