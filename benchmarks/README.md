# Tri Benchmarks

Tri keeps two benchmark families because they answer different questions.

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
