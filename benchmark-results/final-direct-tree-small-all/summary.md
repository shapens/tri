# Agentic Source-Control Benchmark

- Created: `2026-05-29T04:42:48Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-pmwds8o2`
- Attempts: `3`
- Fixture files: `10`
- File size: `256` bytes

| Target | Status | Wall ms | Commands | Requests | I/O bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 140.355 | 0 | 0 | 0 | 15377 |  |
| git | ok | 351.816 | 22 | 0 | 1578 | 47135 |  |
| svn | ok | 5908.955 | 20 | 0 | 6672 | 452050 |  |
| gitbutler | ok | 951.186 | 26 | 0 | 15253 | 236100 |  |
| entire | ok | 3130.047 | 45 | 0 | 13852 | 51585 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
