# Tri Benchmarks

Tri keeps several benchmark families because they answer different questions: the workflow benchmark, the contention benchmark (below), the fan-out disk benchmark (`tri-bench-fanout`, see the top-level README), and the task benchmark.

## Workflow Benchmark

`tri-bench` compares source-control systems on an agent-style coding loop, not only raw command latency.

The default workflow:

1. Create a synthetic source fixture or repository, depending on the target.
2. Start multiple isolated agent attempts from the same base.
3. Read and write files.
4. Run a local check.
5. Produce reviewable work.
6. Compare attempts.
7. Export a patch-like artifact.

Run the default scenario:

```sh
tri-bench
```

Run the small smoke scenario:

```sh
tri-bench --scenario small
```

Run the full trust scenario with repeated medians:

```sh
tri-bench --scenario full
```

Run the scaled adversarial scenario with multiple mutations per attempt, a batched Git baseline, and Tri task verifier outcomes:

```sh
tri-bench --scenario stress
```

Compare Tri transports directly:

```sh
tri-bench --targets tri,tri-cli,tri-http,git,git-batch,svn,gitbutler,entire
```

Workflow scenarios live under `benchmarks/workflow/scenarios/`:

```text
default.json
full.json
small.json
stress.json
```

Outputs:

- `benchmark-results/<timestamp>/results.json`: full machine-readable measurements.
- `benchmark-results/<timestamp>/summary.md`: human-readable summary table.

### Targets

- `tri`: runs one dependent batch plan through Tri's embedded Python API so shell/process startup is not counted as source-control work.
- `tri-cli`: runs the same Tri batch plan through `tri batch` as one subprocess command.
- `tri-http`: runs the same Tri batch plan through a local `tri serve` process and `/v0/batch` request.
- `git`: uses Git worktrees and branches, filesystem edits, local checks, commits, branch comparison, and `git format-patch`.
- `git-batch`: runs the same Git worktree-style workflow inside one generated Python subprocess, so command-startup and agent control-plane overhead do not explain all of Tri's batching advantage. It is a performance stress baseline, not a structured source-control product baseline.
- `svn`: uses local `svnadmin` repositories, branch copies, checkouts, filesystem edits, commits, branch comparison, and `svn diff`.
- `gitbutler`: uses the `but` CLI to set up a GitButler workspace, create parallel branch stacks, commit each attempt, inspect status/show output, write an oplog snapshot, and export through Git compatibility.
- `entire`: uses `entire enable --agent codex`, simulates Codex hook events against local transcript files, creates real local Entire checkpoints, commits each attempt with Git, reads checkpoint/session status, and exports through Git compatibility.

The GitButler and Entire adapters intentionally use their documented automation paths instead of only timing vanilla Git commands with the tools installed beside them.

## Contention Benchmark

`tri-bench-contention` (`benchmarks/contention.py`) measures the two contention rows of the Agent-Scale Scorecard in `tri.md`: merges per hour under contention, and conflicts per 1,000 merges.

The fixture is a generated repository of shared text files (`--files 20` x `--lines 200` by default). `--agents` agents (default 50) run `--parallel` at a time (default 8). Each agent starts from the current main, reads `--edits-per-agent` shared files (default 2), rewrites `--edit-lines` consecutive lines in each (default 3), runs a trivial check, and merges to main. Regions are assigned from a seeded shuffle so edits from different agents never touch and keep two unchanged lines between them; an `--overlap` fraction of agents (default 0.1) instead rewrites exactly the same lines as another agent that starts at about the same time, which is a real conflict for every target when the two race (when one merges before the other starts, the second reads the first's lines and rewrites them knowingly, which is not a conflict).

```sh
tri-bench-contention --agents 50 --parallel 8
tri-bench-contention --agents 4 --parallel 2 --files 3 --lines 30   # smoke size
```

Targets:

- `tri`: `tri workspace create`, `tri files read`/`write`, `tri run`, `tri changeset create`, then `tri changeset merge --target main --retry N` through a merge rule on `src/*`, so no human review. Merges use line-level three-way merges and retry on a moved target; there is no global lock.
- `tri-path`: the same with `TRI_MERGE_TEXT=0`, a benchmark/diagnostic knob that turns line merges off, so any two changesets touching the same file conflict (path-level merging).
- `git`: a bare repository; each agent clones it, edits, runs the same check, commits, and loops `git push` / `git pull --rebase` until the push lands, the rebase conflicts, or the same retry budget (`--retry`, default 5, with the same backoff Tri uses) runs out.

Per target it records merges attempted, succeeded, conflicted, and retry-exhausted; retries; wall time of the agent phase; merges per hour (successful merges over that wall time); conflicts per 1,000 merges (conflicted over attempted); p50 and p95 merge latency (the merge call or push loop alone); line merges performed; conflicts by agents outside the deliberate overlap; and a correctness check of final main: every merged agent's edit is present unless a later merged agent read it and rewrote it, conflicted agents' edits are absent, every other line is the base line, and no file gained or lost lines. A target whose correctness check fails reports `failed`.

Measured on macOS (Apple silicon), 50 agents, 8 at a time, 20 files x 200 lines, overlap 0.1, retry budget 5 (`benchmark-results/contention-50`):

| Target | Attempted | Merged | Conflicted | Retry-exhausted | Retries | Wall s | Merges/hour | Conflicts per 1,000 | p50 merge s | p95 merge s | Correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | 50 | 48 | 2 | 0 | 8 | 6.97 | 24,805 | 40 | 0.132 | 0.203 | yes |
| tri-path | 50 | 29 | 21 | 0 | 0 | 7.12 | 14,659 | 420 | 0.129 | 0.161 | yes |
| git | 50 | 28 | 3 | 19 | 140 | 22.98 | 4,386 | 60 | 2.217 | 5.233 | yes |

Every Tri conflict came from the deliberate overlap (24 merges were line merges of the same file); path-level merging turned 18 disjoint same-file edits into conflicts. Git's rebase is also line-level, so its conflicts are the overlap too, but each retry cycle (fetch, rebase, push) is slow enough that 19 of 50 agents lost the push race six times in a row. Numbers vary run to run with scheduling, mostly in how many overlapping pairs actually race.

Outputs: `results.json` (with per-agent outcomes) and `summary.md` under `benchmark-results/contention-<timestamp>/` or `--output`.

## Task Benchmark

`tri-bench-tasks` runs DeepSWE-style Tri task packages. These tasks are not source-control-system comparisons. They are verifier-backed packages that check whether observable Tri workflows remain correct.

Each task follows this layout:

```text
task.toml         Metadata, timeouts, local environment constraints
instruction.md    Prompt/task description an agent would see
environment/      Dockerfile fallback for isolated execution
tests/            Verifier entry point: test.sh
solution/         Held-out reference outline for reviewers
```

List tasks:

```sh
tri-bench-tasks --list
```

Run all task verifiers:

```sh
tri-bench-tasks
```

Run one task:

```sh
tri-bench-tasks --task tri-conflict-resolution
```

Current tasks:

- `tri-workspace-isolation`: two parallel workspaces mutate the same path without affecting each other or the base snapshot.
- `tri-hidden-path-policy`: hidden path policy redacts private files from listing, search, and diff surfaces.
- `tri-conflict-resolution`: same-path target/workspace divergence records a conflict and requires explicit resolution.
- `tri-export-policy-gates`: export remains blocked until check evidence and review approval exist.
- `tri-secret-lease-redaction`: secret leases inject scoped environment values while redacting logs and blocking revoked reuse.

## Metrics

The workflow benchmark records:

- wall-clock time
- setup, workflow, export, and verifier phase timings
- external subprocess command count
- local HTTP request count
- embedded in-process call count
- local file action count
- agent-visible I/O bytes, including subprocess stdin/stdout/stderr, HTTP request/response/error bodies, and embedded in-process response payloads
- storage size for the target-owned repo/store plus materialized per-attempt working copies or transcripts
- per-step timings
- target-specific details such as Tri object counts or Git branches
- optional Tri task verifier outcomes when `--verify-tasks` or a scenario with `verify_tasks` is used

The task benchmark records:

- verifier pass/fail
- reward
- wall-clock time
- stdout/stderr tails for debugging
- verifier artifacts

Use `--keep-workdir` with either runner when debugging generated repositories or task workdirs.

## Serving Benchmark

`tri-bench-serving` measures requests per second and p50/p99 latency through the HTTP API for files read, files write, workspace create, and changeset propose at 1, 8, and 32 concurrent authenticated clients, comparing `tri serve` with `tri-server` (the Go front server over persistent Python workers). Build the server first (`cd native/tri-server && go build -o tri-server .`).

```sh
tri-bench-serving --output benchmark-results/serving                  # default SQLite journal (DELETE)
tri-bench-serving --journal WAL --no-profile --output benchmark-results/serving-wal
tri-bench-serving --concurrency 1,8 --requests files-read=200,files-write=80   # quick run
```

It also profiles the per-request cost inside Python (Store open, schema creation and migrations, one read with a per-request versus a kept-open store) unless `--no-profile` is passed. The client runs in the same process tree on the same machine, so it shares CPU with the server.
