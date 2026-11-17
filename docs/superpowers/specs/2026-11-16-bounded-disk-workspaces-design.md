# Bounded-Disk Workspaces — Design

Date: 2026-11-16
Status: draft for approval

## Intent

Agents must be able to make isolated changes and *test* them (run real tools against a real directory) while disk usage stays minimal. Git worktrees fail at this structurally: every attempt is a full checkout, every attempt grows its own `node_modules`/`.venv`/build output, and abandoned worktrees linger until someone removes them. Tri should not mitigate this; it should make buildup impossible by construction, on any OS.

## The guarantee

These are the properties the implementation must hold. Everything else serves them.

- **G1 — Disk follows live leases, not attempts or history.** Space used by working directories is a function of currently-live leases only. Creating 1,000 workspaces costs 1,000 database rows. History never costs working-directory space.
- **G2 — Where the filesystem can share, a lease costs what it changes.** On block-cloning or layering filesystems, a live working directory costs roughly the bytes of files it wrote plus per-file metadata. Elsewhere it costs a full copy; that case is reported as `degraded`, never silent.
- **G3 — Dependencies exist once per distinct input.** A dependency tree (`node_modules`, `.venv`, …) is built once per environment key and shared read-only by every run that needs it.
- **G4 — The store grows with unique bytes, and unreferenced bytes are reclaimable.**
- **G5 — Never destroy the only copy of work.** Anything Tri deletes that could hold unsynced edits is first captured into Tri.

## Goals

1. A materializer with capability-probed backends that works on macOS, Linux, and Windows, needs no root, kernel extensions, or drivers, and gives tools an ordinary directory at native speed.
2. A shared, content-keyed dependency environment cache.
3. A lease for every directory Tri creates, with crash-safe cleanup, default expiry, and salvage of unsynced edits.
4. Store garbage collection plus size-budgeted caches.
5. Snapshot metadata that grows with change size, not repo size.
6. Tri itself runs on Windows.
7. A benchmark that proves G1–G3 against git worktrees using allocated (not apparent) bytes, at peak and at the end.

## Non-goals (v1)

- A virtual filesystem (FUSE, NFS loopback, ProjFS). The materializer interface must leave room for one as a later backend.
- Sharing incremental build caches (`target/`, `.next/`, `.pytest_cache`) between attempts; they stay private to a run and die with it. Correctness risk outweighs the win for now.
- Auto-detecting dependency recipes; projects declare them.
- Remote or hosted runners.

## Anti-goals (failure even if it "works")

- Losing edits that existed only in a materialized directory.
- One attempt's writes becoming visible to another attempt, or to the shared caches. Isolation is the point.
- Requiring root, a kernel extension, a driver install, or a special group to get the main benefit.
- Tools behaving differently than in a normal checkout when a clone or overlay backend is available (no symlink tricks on those backends).
- Silent degradation: falling back to full copies without saying so.

## Constraints

- Python ≥ 3.11, standard library only (matches `pyproject.toml`, which declares no dependencies). Platform calls go through `ctypes`, `fcntl.ioctl`, and `subprocess`.
- The existing CLI/HTTP command surface and JSON envelopes stay compatible. New fields are additive.
- Existing stores keep working: schema changes migrate in place (the store already does `ALTER TABLE` migrations in `Store` init), and old flat trees stay readable.
- The existing test suite (`tests/test_tri_cli.py`, `tests/test_benchmark_agentic.py`) keeps passing.
- `tri.md`'s invariants hold, especially "Disk Is A Cache" and "Every Mutation Is Recorded": filesystem edits still come back only through the mutation pipeline.

## Current state (what this changes)

- `cmd_workspace_create` (`tri/cli.py:5523`) already writes only a row. Good; G1 holds for creation.
- `run_check_for_workspace` (`tri/cli.py:7914`) materializes a full copy into `.tri/runs/<check>/work` via `materialize_tree` (`tri/cli.py:2472`), then rescans and reads every file via `build_entries_from_dir` to find changes. It deletes the run dir with `shutil.rmtree` only on the normal path (`tri/cli.py:8083`); an exception or crash leaks it.
- `cmd_workspace_mount` (`tri/cli.py:5807`) writes a full copy anywhere; TTL is optional; nothing ever reaps expired mounts. `discard`/`archive` (`tri/cli.py:5799`) only flip status.
- Blobs are content-addressed and individually zlib-compressed (`put_blob`, `tri/cli.py:943`). There is no GC.
- Trees are a single flat, compressed JSON map of every path (`encode_tree_json`, `tri/cli.py:381`), and a snapshot is written after every mutation, so metadata grows O(files × mutations).
- `tri/cli.py` imports `fcntl` unconditionally and uses `flock` for the store lock, so Tri does not start on Windows.
- The benchmark measures `directory_size` at the end (`benchmarks/agentic.py:686`): apparent bytes, final state only. It would count clones as full copies and miss peaks.

## Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| A | **Copy-on-write materializer with probed backends** (block clones on APFS/btrfs/XFS/ReFS, overlay for Linux runs, copy fallback) + shared env cache + leases + GC | Native speed, every tool works, no privileges. Degrades to full copies on filesystems that can't share (e.g. ext4 outside user namespaces, NTFS), where G1 still bounds total use by concurrency | **Chosen** |
| B | Virtual filesystem serving the store lazily (EdenFS / VFS for Git model) | Truly zero-copy, but needs a driver or privileged mount on every OS, stat-heavy builds slow down, largest and riskiest build, hard to make reliable from Python | Rejected for v1; future backend behind the same interface |
| C | Keep ephemeral full copies; add dep cache + reaper only | Simplest, universal, but every run still writes O(repo) bytes and time. Mitigation, not a fix | Rejected |
| — | Writable hardlink farm (pnpm-style) | In-place writes and `chmod` mutate the shared inode, corrupting content for every other attempt | Rejected. Hardlinks are allowed only into read-only overlay lower layers, which overlay never writes to |

The user delegated the choice of approach; A is the design's call.

## Design

### 1. Checkout cache

A directory of raw (uncompressed), read-only blob files at `.tri/checkout/<hash[:2]>/<hash>`, populated lazily the first time a blob is materialized, on the same volume as the store. It is the clone source and the hardlink source for overlay lower layers.

- It is a cache: evictable under a size budget (LRU, never evicting a blob referenced by a live lease's base), rebuildable from blobs.
- Cost: one raw copy per unique blob, shared across all workspaces — about one checkout's worth total, independent of attempt count.
- Builder may instead unify it with the blob store on clone-capable volumes (store raw blobs there). Either is fine if G2–G4 hold.

### 2. Snapshot base directories

For backends that benefit, a read-only, fully materialized directory per base snapshot at `.tri/bases/<snapshot>/`, built from the checkout cache by per-file clones (clone backend) or hardlinks (overlay backend, where it is a read-only lower layer). Built on demand, kept for the most recently used snapshots under a budget, deleted when no live lease uses them.

On APFS this enables a whole-tree `clonefile(2)` of the directory in one syscall, which matters for trees with tens of thousands of files.

### 3. Materializer interface

One internal interface replaces direct `materialize_tree` calls:

```
materialize(entries, dest, purpose: "run" | "mount", writable: bool) -> Materialization
Materialization: backend, path, degraded: bool, changed_paths() -> (written, deleted), release()
```

`changed_paths()` replaces the full rescan in `build_entries_from_dir` for detecting changes. Only changed files get read and hashed.

Backends, tried in order of preference for the purpose and volume:

| Backend | Where | Purpose | How it isolates | Change detection |
|---|---|---|---|---|
| `overlay` | Linux, when an unprivileged user + mount namespace works (or running as root) | `run` only — the mount exists only inside the namespace the command runs in | lower = base dir (+ env layers), upper/work = per run | Walk upper dir; whiteouts are deletes |
| `clone` | APFS (`clonefile`), btrfs / XFS / bcachefs / ZFS with block cloning (`FICLONE` ioctl), ReFS / Dev Drive (`FSCTL_DUPLICATE_EXTENTS_TO_FILE`) | `run` and `mount` | Block-level copy-on-write; writes allocate new blocks privately | Record `(size, mtime_ns)` per path at materialize time; stat walk after, hash only mismatches |
| `copy` | Anywhere | `run` and `mount` | Real copy | Same stat-based detection as `clone` |

Rules:

- **Probe, don't guess.** Capability is determined by actually trying (clone a temp file in the destination volume; attempt an overlay mount in a namespace) and cached per host + volume (device id) in the store. `tri doctor` reports the result per volume.
- **Always record which backend served a lease or check** (`backend`, `degraded`) on the lease and check-run records. Additive fields.
- If the destination is on a different volume than the checkout cache, `clone` is impossible: use `copy` and report `degraded`. (Builder may add a per-volume cache later.)
- Ubuntu 23.10+ AppArmor and Docker's default seccomp profile often block unprivileged user namespaces. The probe handles this by falling through to `clone` or `copy`; it is not an error.
- Stat-based detection must treat mtime granularity correctly: a file modified within the same mtime tick as materialization must still be detected. Builder picks the mechanism (e.g. set materialized mtimes to a fixed past value, or compare inode + ctime too).

### 4. Environment cache (dependencies)

Projects declare recipes:

```
tri env define <project> <name> \
  --key-file package-lock.json [--key-file .nvmrc ...] \
  [--key-command "node --version" ...] \
  --install "npm ci" \
  --output node_modules [--output packages/web/node_modules ...] \
  [--mode auto|copy|link]
```

- **Key** = hash of the recipe, the content of the key files *at the run's snapshot*, the output of the key commands, and a platform tag (OS, arch, libc). If an attempt changes its lockfile, it gets a new key, built once and shared by any other attempt with the same lockfile.
- **What the install sees:** the env build materializes the *full* snapshot (through the materializer, so it is cheap) into a build directory and runs the install command there, so `package.json`, workspace manifests and postinstall scripts all work. The declared outputs are then harvested into `.tri/envs/<key>/` and frozen read-only.
- **Key contract:** the recipe's key files and key commands must cover every input that changes the install's result. Tri can't verify this in general. The docs for `tri env define` say so plainly, and the key is recorded on every run that used the env, so a wrong shared env is traceable.
- **Fixed location, never relocated:** an env lives at `.tri/envs/<key>/` for its whole life. Absolute paths written by installers (venv shebangs, `pyvenv.cfg`) therefore keep resolving to the shared, read-only copy. An env is evicted only when no live lease (run or mount) references it.
- **Self-referential installs** that point back into the source tree (`pip install -e .`, npm/pnpm workspace links to sibling packages) don't fit a shared env. On `clone` and `overlay`, relative workspace links resolve inside the run directory and work. Editable installs, and any recipe using `--mode private`, are installed per run instead and die with the run. The design's call is not to try to detect these automatically in v1.
- Builds are recorded as check runs (kind `env.build`) with the same provenance, secret-lease, and policy handling as `tri run`. A per-key lock makes concurrent runs that need the same missing key wait for one build.
- **Use in runs:** outputs are attached via the materializer: as an extra overlay lower layer, a directory clone (`clone`), or, on `copy`, a read-only symlink to the shared env (`--mode auto`). `--mode copy` forces a private copy for tools that refuse symlinked directories.
- **Use in mounts:** mounts get envs too, because mounts are where agents work interactively. They are attached by directory clone when the volume supports it, so the agent can still run `npm install` in the mount and change its private copy. On `copy`, mounts don't get a shared env by default: a read-only symlink would break an agent's own installs. `--env-link` opts in.
- Env outputs are always excluded from change import, sync, and salvage, so dependencies never land in snapshots.
- A run whose env build fails fails with a structured diagnostic pointing at the `env.build` check.

### 4a. What a run imports back

Today a run imports every new file its command creates, so `__pycache__`, `target/`, `.pytest_cache` and coverage output land in workspace snapshots. That contradicts G4 and the "build caches die with the run" non-goal.

- Change import (runs, sync, salvage) honors the `.gitignore` files in the snapshot being worked on, plus project-declared patterns (`tri ignore add <project> <glob>`), plus env outputs and generated config paths.
- Ignored files die with the run directory.
- This is a deliberate behavior change. Existing tests that depend on importing ignored files get updated, and the change is called out in the README.

### 5. Leases and lifecycle

Every directory Tri creates on disk is owned by a lease row: `run` (ephemeral), `mount`, `env-build`, `base`.

- **Runs:** acquired before materializing, released in a `finally`. The lease records the holder's PID and process start time, the before-snapshot, and a pointer to the persisted change-detection baseline (written to disk next to the run dir, not held only in memory). A reaper can then tell a crashed run from a live one, and salvage it after the owner is gone.
- **Mounts:** new mounts default to a 24-hour TTL (design's call; configurable). `tri workspace sync` and `tri leases renew <lease>` each extend it by the TTL. An expired mount is reaped.
- **Existing mounts are grandfathered:** mount leases created before this change with no expiry are never auto-reaped. `tri doctor` and `tri leases list` show them, so the user can renew them with a TTL or release them. Tri never starts deleting directories a user created weeks ago.
- **Salvage before delete (G5):** before reaping a mount or an orphaned run dir, or when a workspace with live mounts is discarded or archived, Tri compares the directory to the lease's baseline. If anything changed, it captures what `sync` would capture (section 4a rules) as a **salvage snapshot** attached to the workspace (head does not move) and emits `lease.salvaged` with the snapshot id. Users or agents can adopt the salvage snapshot explicitly.
- **What salvage never deletes:** files Tri can't capture and didn't create. If an ignored path or a secret-like path (by the existing export secret-path rules) contains files that weren't written by Tri (materialized, env-attached, or generated config), Tri doesn't capture them into the blob store and doesn't delete them. The lease moves to `expired_needs_attention`, shown by `tri doctor` and `tri leases list`. `tri leases reap <lease> --force` deletes it. Ignored build output inside an *orphaned run* dir is Tri-created by definition and is deleted.
- A mount whose directory the user already deleted is marked `gone`.
- **Reaper triggers:** `tri gc --reap`, opportunistically on store open when the last reap is older than a threshold (bounded amount of work per invocation), and periodically inside `tri serve`. No new daemon.

### 6. Garbage collection and budgets

- **Caches (automatic):** checkout cache, base dirs, and env cache each have a size budget (configurable; builder picks defaults) and are evicted LRU, never evicting anything a live lease uses.
- **Concurrency:** GC must work while `tri serve` and long `tri run`s are live. Every `Store` holds a shared `flock` for its whole lifetime (`tri/cli.py:481`), so GC can't wait for the exclusive store lock. Instead:
  - GC takes its own GC lock.
  - It sweeps only objects created before the mark phase started.
  - Writers record a last-referenced time when `put_blob`/`put_tree` dedup hits an existing object, and the sweep skips anything referenced after the mark started.
  - `put_blob` rewrites a blob whose file is missing instead of assuming it exists.
  - The builder picks the exact mechanism; the constraint is no whole-store exclusive lock and no lost objects under concurrent writes.
- **Store content (explicit):** `tri gc` computes a plan and prints it; `tri gc --apply` executes it.
  - Roots: every snapshot referenced by targets (current and history), non-discarded workspaces (base, head, operation history), changesets, check runs (before/after), conflicts, reviews, exports, releases, and salvage snapshots.
  - Discarded workspaces and discarded/superseded changesets stop being roots after a retention window (default 14 days, design's call; configurable).
  - Unreachable blobs and trees lose their content. Their rows stay, marked purged, so ids still resolve to a clear `content_purged` error instead of `path_not_found`. Metadata and events are never deleted, preserving audit.

### 7. Hierarchical trees

Replace flat per-snapshot path maps with directory-level, content-addressed tree objects, so one edit writes O(depth) new tree nodes instead of O(files).

- New trees carry a format version; existing flat trees stay readable.
- `load_snapshot_tree` keeps returning the flattened map, so callers are unchanged, with a cache for hot snapshots.
- Diff can then skip identical subtrees by hash; builder may adopt that where it is simple.

### 8. Windows

- Replace `fcntl.flock` with a cross-platform store lock (e.g. `msvcrt.locking` on Windows) keeping the current shared/exclusive semantics.
- Handle symlink creation without privileges (Windows requires Developer Mode or elevation): fall back to a clear diagnostic per entry rather than crashing.
- `clone` backend via ReFS / Dev Drive block cloning; `copy` elsewhere. Overlay is not available. ProjFS is a future virtual-filesystem backend, not v1.
- **Done means:** the existing suite passes on Windows, with tests that need unprivileged symlinks skipped there with a stated reason.

### 8a. Platform verification

- Done for every step means the full suite passes on macOS and Linux.
- Backend-specific tests run against the real backend where the machine has it (APFS on macOS; btrfs or XFS loopback images and user namespaces on Linux CI runners with sudo; Dev Drive or ReFS on Windows when available). Elsewhere they skip with the reason.
- Fallback paths (`copy`, failed probes) are tested everywhere by forcing the probe result.
- The project has no CI today. Adding a macOS/Linux/Windows matrix (e.g. GitHub Actions) is part of this work once the project is in a git repo.

### 9. Measurement

A new benchmark scenario, `fanout`, that reflects agent reality:

- Fixture: a few thousand source files plus a dependency recipe whose install command deterministically generates a large tree (e.g. ~20k files) offline, so no network.
- N attempts (e.g. 10 and 50), each editing a few files and running a check, with some concurrency.
- Compared against git worktrees, where each worktree runs the same install.
- Report **allocated** bytes at **peak and end**, plus setup and run wall time. Apparent sizes (`du`, `directory_size`) double-count clones, so measure free-space deltas (`statvfs`) on a dedicated volume where possible (e.g. a sparse APFS disk image on macOS, a loopback btrfs/ext4 image in Linux CI), and fall back to summed `st_blocks` with the limitation stated.
- Report which backend served each run.

**Success criteria:**

- On `clone`/`overlay` backends with 50 attempts, peak Tri disk use is within about 1.5× of a *single* git worktree including its dependencies, while git worktrees grow roughly linearly with attempts.
- On `copy`, peak disk use is bounded by concurrent runs, and final disk use (after runs finish) does not grow with attempt count.
- Leaked run directories after a killed run are reclaimed by the next reap.
- No test in the existing suite regresses.

## Build order (builder may adjust)

1. Measurement (`fanout` scenario, allocated-bytes metrics) — establish the baseline numbers first.
2. Leases for every directory, `finally`-based release, crash reaper, default mount TTL, salvage on reap/discard/archive.
3. Materializer interface, checkout cache, `clone` backends (APFS with directory clone, Linux `FICLONE`), `copy` fallback, stat-based change detection, backend reporting in leases, checks, and `tri doctor`.
4. Environment cache.
5. `overlay` backend for Linux runs.
6. Cache budgets and `tri gc`.
7. Hierarchical trees.
8. Windows port and ReFS/Dev Drive clone.

## Decisions made in this design (not by the user)

- Approach A over a virtual filesystem (user delegated the choice: "do what's best, support any system").
- Standard library only.
- Default mount TTL 24h; GC retention 14 days; history purge explicit (`--apply`), cache eviction automatic.
- Salvage snapshots do not move the workspace head.
- Env outputs use symlinks on the `copy` backend for runs by default; mounts on `copy` get no shared env unless `--env-link` is passed.
- Env builds see the full snapshot; recipes are responsible for declaring complete keys; envs live at a fixed path and are never relocated.
- Incremental build caches stay private per run in v1.
- Run import, sync, and salvage honor `.gitignore` plus project ignore patterns (behavior change).
- Existing no-expiry mounts are grandfathered; sync renews mount leases.
- Salvage never captures or deletes files that aren't Tri-created in ignored or secret-like paths; those leases wait for the user.
- Orphaned run dirs are salvaged like mounts, from a persisted baseline.
- GC runs alongside live servers and runs, with no whole-store exclusive lock.
- Windows done = existing suite passes there, with symlink tests skipped.

## Left to the builder

- `ctypes`/ioctl bindings and probe details; probe cache invalidation.
- Exact schemas for new tables and columns.
- Budget defaults and reaper thresholds.
- Whether to unify the checkout cache with the blob store on clone-capable volumes.
- Benchmark volume setup per platform.
- mtime-granularity handling in stat-based change detection.
