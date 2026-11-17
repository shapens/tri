# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:31:55Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-jwpid_q3`
- Attempts: `3`
- Fixture files: `10`
- File size: `256` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 416.843 | 0 | 0 | 0 | 15378 |  |
| git | ok | 1048.000 | 22 | 0 | 1578 | 47136 |  |
| svn | ok | 6554.677 | 20 | 0 | 6672 | 452050 |  |
| gitbutler | ok | 3744.198 | 26 | 0 | 15253 | 236117 |  |
| entire | ok | 6204.531 | 45 | 0 | 13853 | 51574 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
