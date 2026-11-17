# Agentic Source-Control Benchmark

- Created: `2026-05-29T01:40:56Z`
- Workdir: `/var/folders/2p/hwpnst4x41g1jqgt3hrbz_680000gp/T/tri-agentic-bench-26qkhhtv`
- Attempts: `3`
- Fixture files: `100`
- File size: `1024` bytes

| Target | Status | Wall ms | Commands | Output bytes | Store bytes | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | 3319.943 | 26 | 143257 | 520422 |  |
| git | ok | 464.244 | 22 | 5000 | 180587 |  |
| svn | ok | 5969.725 | 20 | 48162 | 106882 |  |
| gitbutler | ok | 1342.414 | 26 | 18675 | 363551 |  |
| entire | ok | 2941.104 | 45 | 14248 | 175190 |  |

## Interpretation

This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.

Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.
