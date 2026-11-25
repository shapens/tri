# Tri

Agent-native source control and copy-on-write workspaces.

Tri replaces shared working directories and Git worktrees with immutable snapshots, API-first workspaces, bounded-disk copy-on-write checkouts, sandboxed command runs, object-level visibility policies, scoped secret leases, and two-way Git compatibility. Workspaces are database rows rather than full checkouts on disk; directories are materialized only while a command runs or a mount is leased.

Zero external runtime dependencies: the core CLI, worker, PostgreSQL wire client, S3 SigV4 client, and Linux FUSE server use standard-library Python 3.11+, and the native front server (`native/tri-server`) uses standard-library Go 1.22+.

## Architecture

```text
CLI / MCP / HTTP / RL Harness
             │
             ▼
  tri-server (Go front server)    ── bearer auth, queueing, timeouts, /v0/ready, /metrics
             │  length-framed JSON over stdio
             ▼
  tri worker pool (Python)        ── policy, batches, contracts/v0, line-level 3-way merges
      ┌──────┴──────────────────┐
      ▼                         ▼
Storage                     Execution & Views
SQLite (WAL) / Postgres     APFS / XFS / btrfs / ReFS clones, Linux overlayfs & FUSE
Local blobs / S3            Seatbelt, bubblewrap, gVisor (runsc), Kata, Windows Job Objects
```

For the full object model, invariants, and migration plan, see [docs/design.md](docs/design.md).

## Quickstart

Install the CLI in editable mode:

```sh
python3 -m pip install -e .
```

Import a repository, create an isolated workspace, write files atomically, run a sandboxed check, and inspect the diff:

```sh
export TRI_HOME=/tmp/tri-home
tri import ./repo --name app
WS=$(tri workspace create app --from main | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['id'])")
tri files write-batch "$WS" --entries-json '[{"path":"hello.py","text":"print(42)\n"}]'
tri run "$WS" --sandbox -- python3 hello.py
tri diff "$WS"
```

Connect an agent harness over MCP (stdio), pinned to one actor:

```sh
claude mcp add tri -- env TRI_HOME=/tmp/tri-home tri --actor agent:claude mcp
```

## High-Concurrency Serving and RL Grading

Build the Go front server (`native/tri-server`) for process-level concurrency, graceful drain, request/queue timeouts, `/v0/ready`, and Prometheus `/metrics`:

```sh
cd native/tri-server && go build -o tri-server . && cd -
TRI_HOME=/tmp/tri-home native/tri-server/tri-server \
  --host 127.0.0.1 --port 8765 --workers 16 --run-sandbox required \
  -- python3 -m tri worker --stdio
```

Both `tri serve` and `tri worker` default to SQLite `WAL` mode with `IMMEDIATE` transactions and HTTP/1.1 keep-alive. Dependent multi-step workflows (such as RL episode grading: create workspace, atomic `files write-batch`, sandboxed `run`, stream stdout/stderr, discard workspace) collapse into a single `POST /v0/batch` request using `${step.data.id}` placeholders.

Measured on Apple Silicon under mandatory macOS Seatbelt isolation (`--run-sandbox required`) via [shapens/loop](https://github.com/shapens/loop) (`50` gradings per concurrency point, `0` errors through `c=64`):

| Concurrency | Throughput | p50 Latency | p95 Latency |
| ---: | ---: | ---: | ---: |
| 1 | 15.0 /s | 66.5 ms | 205.8 ms |
| 8 | 47.8 /s | 162.8 ms | 241.0 ms |
| 16 | 54.5 /s | 209.8 ms | 506.4 ms |
| 32 | 46.0 /s | 692.6 ms | 1,039.2 ms |
| 64 | 39.3 /s | 1,648.6 ms | 2,403.5 ms |

For authenticated non-loopback serving, issue per-actor bearer tokens (`tri auth issue agent:a --file tokens.json`) and pass `--auth-file tokens.json`. Non-loopback servers refuse to start without `--auth-file` and require sandboxed runs by default.

## Bounded-Disk Workspaces

`tri workspace create` writes no files. When a run or mount needs a directory, Tri selects the cheapest safe materializer on the host (`TRI_MATERIALIZE_BACKEND=auto|clone|overlay|copy`):

| Backend | Platforms | Behavior |
| :--- | :--- | :--- |
| `clone` | APFS (macOS), XFS / btrfs / ZFS (Linux `FICLONE`), ReFS (Windows) | Copy-on-write clones from a base checkout cache; only workspace deltas write new blocks. |
| `overlay` | Linux (unprivileged user + mount namespaces) | Ephemeral overlay mount inside the command's namespace; only written files land on disk. |
| `copy` | Any other filesystem (ext4, NTFS, cross-volume) | Plain file copy, explicitly flagged as `degraded` in `tri doctor`, checks, and leases. |

Shared dependencies (`tri env define`) are built once per lockfile key, frozen read-only, and cloned or layered into each run without being imported into snapshots. Mounts carry expiring leases (default 24h) and salvage unsynced edits before reaping (`tri salvage list`, `tri gc --reap`, `tri gc --apply`).

On Linux with `fuse3`, `tri workspace mount <ws> ./wt --virtual` exposes a whole workspace without naming paths up front, faulting files in on first read and copying edits up on write. On macOS and Windows, use `--paths <glob>` for partial checkouts.

## Sandbox Isolation

| Platform | Default Backend | Network Denied | Notes |
| :--- | :--- | :--- | :--- |
| Linux | `bubblewrap` (`bwrap`) | Yes | Read-only host, writable run dir only, `TRI_HOME` and credential dirs hidden, scrubbed env, CPU/memory/fsize limits. |
| Linux (OCI) | `container` (`runc`, `runsc` gVisor, `kata-fc`) | Yes | Opt in via `TRI_RUN_SANDBOX_BACKEND=container` and `TRI_RUN_OCI_RUNTIME`. |
| macOS | Seatbelt (`sandbox-exec`) | Yes | Read-only host, writable run dir only, `TRI_HOME` and credential dirs hidden, scrubbed env, limits. |
| Windows | Low-integrity Job Object | No | Kills process tree on close with memory/CPU/process limits; `network_enforced: false` is recorded on every run. |

See [SECURITY.md](SECURITY.md) for the full threat model and known boundaries.

## Replicated Storage, Remote Runners, and Git Mirror

Stateless `tri serve` / `tri-server` nodes can share PostgreSQL metadata (`TRI_DATABASE_URL`, `TRI_DATABASE_SCHEMA`) and S3-compatible or shared-directory object storage (`TRI_OBJECT_STORE`). Clients accept comma-separated `TRI_REMOTE` URLs for failover, and `tri storage migrate` / `tri backup create` convert between SQLite and replicated stores.

To move execution off the store host, run `tri serve --run-executor remote` and supervise isolated runners with `tri runner autoscale --min 1 --max 8`. Jobs carry workspace-scoped secret leases (`--with-lease`) delivered only to the active claim holder and redacted from logs. For incremental team adoption, `tri mirror add` and `tri mirror sync` maintain a two-way Git mirror while keeping private packages and secret-like paths inside Tri.

## Benchmarks

Benchmarks live under `benchmarks/` (see [benchmarks/README.md](benchmarks/README.md) for methodology and CLI flags). Summary of recorded runs on Apple Silicon (and Docker Linux VM for FUSE):

| Benchmark | Workload | Tri | Baseline |
| :--- | :--- | :--- | :--- |
| `tri-bench-fanout` | 50 attempts (1k src + 2k dep files, APFS) | 30.4 MiB (`~43 KiB`/extra) | 672.6 MiB git worktrees (`~13.3 MiB`/extra) |
| `tri-bench-fanout` | 100 attempts, 8 parallel (2k src + 5k dep) | 63 MiB, 150 s | 3.0 GiB git worktrees, 158 s |
| `tri-bench-contention` | 50 agents, 8 parallel, 20 shared files | 24,805 merges/hr (48/50 merged, 40 conflicts/1k) | 4,386 merges/hr git pull-rebase-push (28/50 merged) |
| `tri-bench-serving` | 32 auth clients, 8 workers (`tri-server` WAL) | 1,960 req/s reads (p99 67 ms), 263 req/s writes | 156 req/s reads, 69 req/s writes (`tri serve`) |
| `tri-bench-virtual` | 50,000-file tree (Linux FUSE view) | 2.0 s local / 3.1 s remote first read (1–33 KiB) | 5.9 s / 92.8 s full checkout (49 MiB) |

## Verification and Contracts

`contracts/v0/` contains generated JSON Schemas for every command, route, envelope, and diagnostic code. Every slice of `native/tri-server` is verified against `tri serve` through the black-box conformance and leak-sweep suites.

```sh
python3 -m unittest discover -s tests -v
python3 -m tri.contracts --check
(cd native/tri-server && go vet ./... && go test -race -count=1 ./...)
TRI_CONFORMANCE_NATIVE="$PWD/native/tri-server/tri-server" \
  python3 -m unittest discover -s tests/conformance -t tests -v
TRI_CONFORMANCE_NATIVE="$PWD/native/tri-server/tri-server" \
  python3 -m unittest discover -s tests -p test_leak_sweep.py -v
```

CI runs across Ubuntu, macOS, and Windows, including Linux gVisor (`runsc`), overlayfs + bubblewrap, `fuse3` virtual checkouts, XFS/btrfs reflinks, Windows ReFS clones, PostgreSQL + S3 replicated storage, and `tri-server` race/conformance checks.

## Current Limitations

SQLite stores serialize writes behind one writer per store; PostgreSQL stores use per-workspace/per-target scoped advisory locks for concurrent workspace and merge writes while serializing store-wide operations (GC, migrations). Virtual FUSE checkouts are Linux-only (`--paths` partial checkouts are used on macOS and Windows). Firecracker microVMs are configured through Kata (`TRI_RUN_OCI_RUNTIME=kata-fc`) and are not exercised in GitHub Actions CI because runners lack KVM. Most command logic, policy evaluation, and storage remain in the Python worker pool behind the Go front server.

## License

Apache License 2.0. See [LICENSE](LICENSE).
