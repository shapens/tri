# Bounded-Disk Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Tri's on-disk working directories cost what they change (where the filesystem allows), share dependency trees, and never accumulate — on macOS, Linux, and Windows.

**Architecture:** New focused modules (`tri/locking.py`, `tri/ignore.py`, `tri/materialize.py`, `tri/overlay.py`, `tri/trees.py`) hold the platform and algorithm code; `tri/cli.py` wires them into the store, runs, mounts, leases, env cache, GC, and commands. Modules never import `tri.cli`.

**Tech Stack:** Python 3.11 stdlib only (`ctypes`, `fcntl.ioctl`, `subprocess`, `sqlite3`), unittest.

**Spec:** `docs/superpowers/specs/2026-11-16-bounded-disk-workspaces-design.md`

## Global Constraints

- Python ≥ 3.11, standard library only; no new dependencies in `pyproject.toml`.
- CLI/HTTP envelopes stay compatible; new data fields are additive. Every new command gets a `COMMAND_CONTRACTS` entry and a `SCHEMA_CATALOG` schema.
- Existing stores open and migrate in place (`Store.migrate_db`); flat trees stay readable.
- Existing 72 tests keep passing (`python3 -m unittest discover -s tests`).
- `TRI_MATERIALIZE_BACKEND=auto|clone|copy|overlay` overrides the probe (tests and users).
- Default mount TTL 86400 s; GC retention 14 days; GC grace 3600 s; checkout/base/env cache budgets 2 GiB / 8 bases / 10 GiB (configurable via env vars `TRI_CHECKOUT_CACHE_MAX_BYTES`, `TRI_BASE_CACHE_MAX_COUNT`, `TRI_ENV_CACHE_MAX_BYTES`).
- Materialized files get mtime `946684800` (2000-01-01 UTC) so any write is detectable and zip-based tools still accept the timestamp.

## Review Focus

1. A run whose command edits a file without changing its size or that replaces it via rename — must still be imported (Task 3 tests same-size edit and rename-replace).
2. Executable and symlink entries round-tripping through a run unchanged — must not show up as changes (Task 3).
3. A tracked file living under a gitignored directory — edits to it must still be imported (Task 2/4).
4. Two runs needing the same missing env key concurrently — one build, both succeed (Task 6).
5. GC racing a writer that dedups against an unreferenced blob — the blob must survive or be rewritten (Task 8).

---

### Task 1: Cross-platform store lock

**Files:** Create `tri/locking.py`; Modify `tri/cli.py` (`import fcntl`, `Store.__init__`, `Store.close`); Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** `class FileLock(path: Path)` with `acquire(*, exclusive: bool, blocking: bool = True) -> bool`, `release() -> None`, `close() -> None`. POSIX uses `fcntl.flock`; Windows uses `LockFileEx`/`UnlockFileEx` via `ctypes` (shared vs exclusive preserved).

- [ ] Test `test_file_lock_shared_then_exclusive_nonblocking`: two `FileLock`s on one path; both shared → True; a third exclusive non-blocking → False; after releases → True.
- [ ] Implement, replace every `fcntl.flock` in `cli.py`, drop `import fcntl` from `cli.py`.
- [ ] Full suite passes.

### Task 2: Ignore rules

**Files:** Create `tri/ignore.py`; Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** `class IgnoreRules` with `IgnoreRules.from_entries(entries: dict[str, dict], read_text: Callable[[str], str | None], extra_patterns: list[str]) -> IgnoreRules` and `matches(path: str, is_dir: bool = False) -> bool`. Gitignore semantics: comments, blank lines, `!` negation, trailing `/` dir-only, leading or inner `/` anchors to the `.gitignore`'s directory, `*`, `?`, `[...]`, `**`; nested `.gitignore` files; a path is ignored if any parent directory is ignored.

- [ ] Tests: `test_ignore_basic_globs`, `test_ignore_negation_and_dir_only`, `test_ignore_anchored_and_nested`, `test_ignore_parent_dir_ignores_children` (`node_modules/` ignores `node_modules/a/b.js`; `/build` ignores `build/x` but not `src/build/x`; `!keep.log` after `*.log`).
- [ ] Implement by compiling each pattern to a regex.

### Task 3: Materializer core (probe, clone, checkout cache, bases, change detection)

**Files:** Create `tri/materialize.py`; Test `tests/test_bounded_disk.py`

**Interfaces — Produces:**
- `FIXED_MTIME = 946684800`
- `probe_clone(directory: Path) -> str | None` returns `"clonefile"` (macOS), `"ficlone"` (Linux), `"refs"` (Windows) or `None`.
- `clone_file(src: Path, dst: Path, method: str) -> None`; `clone_dir(src: Path, dst: Path) -> bool` (macOS `clonefile` on a directory; False elsewhere).
- `class CheckoutCache(root: Path, read_blob: Callable[[str], bytes])` with `path_for(hash: str, blob_id: str, executable: bool) -> Path` (raw file, mode 0644/0755, mtime `FIXED_MTIME`, atomic create).
- `class Materializer(home: Path, cache: CheckoutCache, read_blob)` with `materialize(entries, dest: Path, *, base: tuple[str, dict] | None = None, attachments: list[Attachment] = (), backend: str | None = None) -> Materialization`.
- `Attachment(rel: str, source: Path, mode: "clone" | "link" | "copy")` for env outputs.
- `class Materialization`: `path`, `backend`, `degraded: bool`, `baseline: dict[str, list]`, `skip_prefixes: list[str]`, `save_baseline(file: Path)`, `scan(ignore: Callable[[str, bool], bool], tracked: set[str]) -> tuple[list[str], list[str]]` (changed-or-new, deleted), `release()`.
- `load_materialization(path: Path, baseline_file: Path) -> Materialization` for later processes (sync, reaper).
- `scan_directory(root, baseline, entries, ignore, skip_prefixes) -> (changed, deleted)` — a path is changed when `(size, mtime_ns, ino, mode bits)` differ from baseline or it is new; callers confirm by hash.

- [ ] Tests (real clone on this Mac, forced copy too): `test_materialize_clone_and_copy_produce_identical_trees` (content, exec bit, symlink, mtime == FIXED_MTIME), `test_scan_detects_same_size_edit`, `test_scan_detects_rename_replace`, `test_scan_detects_delete_and_new`, `test_scan_skips_ignored_new_files_but_not_tracked`, `test_clone_backend_shares_blocks` (macOS: APFS free-space delta of 20 clones of a 20 MB file < 5 MB), `test_base_dir_reused_across_materializations`.
- [ ] Implement; `clonefile` via `ctypes.CDLL(None).clonefile`; `FICLONE = 0x40049409` via `fcntl.ioctl`; Windows `FSCTL_DUPLICATE_EXTENTS_TO_FILE = 0x98344` via `DeviceIoControl`. Base dirs at `<home>/bases/<tree_id>/tree`, built atomically (tmp + rename), used via `clone_dir` when available, then the head-vs-base delta applied.

### Task 4: Wire materializer into runs, revalidation, mounts, sync; import rules; reporting

**Files:** Modify `tri/cli.py` (`run_check_for_workspace`, `run_check_for_changeset_head`, `cmd_workspace_mount`, `cmd_workspace_sync`, `materialized_directory_record`, `cmd_doctor`, `materialization_lease_data`, migrations, parser, contracts); Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** `Store.materializer` (lazy); `import_rules(store, project_id, entries) -> IgnoreRules` (gitignore + `project_ignores` table); `entries_after_scan(store, before_entries, mat, rules, extra_skip) -> (after_entries, paths)`; commands `tri ignore add|list|remove <project> [<glob>]`. Check `materialized_directory` and mount leases gain `backend`, `degraded`. `tri doctor` gains `data.materialization` = `{store_volume: {backend, clone_method, overlay}, caches: {...}, leases: {...}}`.

- [ ] Tests: `test_run_does_not_import_gitignored_outputs`, `test_run_imports_edit_to_tracked_file_under_ignored_dir`, `test_run_preserves_executable_mode`, `test_check_reports_backend`, `test_sync_uses_baseline`, `test_doctor_reports_materialization`, `test_ignore_commands`.
- [ ] Implement; full suite passes.

### Task 5: Leases, reaper, salvage

**Files:** Modify `tri/cli.py`; Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** tables `disk_leases(id, kind, path, workspace_id, check_id, before_snapshot_id, baseline_path, backend, status, created_at, released_at)`, `salvages(id, workspace_id, lease_id, kind, base_snapshot_id, snapshot_id, paths_json, deleted_ignored_json, created_at, adopted_at)`, `store_meta(key, value)`; materialization_leases columns `backend, degraded, ttl_seconds, baseline_path, attention_json, reaped_at, salvage_id`. Functions `reap_leases(store, *, actor, limit=None, force_lease=None) -> dict`, `maybe_auto_reap(store)`. Run dirs hold `FileLock(<run>/.holder.lock)` exclusively while live. Commands: `tri leases list [--workspace] [--status]`, `tri leases renew <lease> [--ttl-seconds]`, `tri leases reap [<lease>] [--force]`, `tri salvage list <workspace>`, `tri salvage adopt <salvage> [--expect-head]`. Mount: default TTL 86400, `--no-ttl`; sync renews. Discard/archive reap that workspace's mounts. Auto-reap after CLI commands when last reap > 600 s (`TRI_AUTO_REAP=0` disables); `tri serve` reaps every 300 s.

Salvage rules: changes outside ignore rules → salvage snapshot (parents `[last_sync]`, head untouched, event `lease.salvaged`); non-baseline secret-like files → `expired_needs_attention`, nothing deleted; ignored non-secret files Tri didn't create are deleted and listed in `deleted_ignored_json` (refinement of spec §5, see report).

- [ ] Tests: `test_orphaned_run_dir_is_salvaged_and_removed`, `test_expired_mount_salvaged_then_deleted`, `test_secret_like_file_blocks_reap`, `test_grandfathered_mount_not_reaped`, `test_sync_renews_lease`, `test_discard_reaps_mounts`, `test_salvage_adopt`.
- [ ] Implement; full suite passes.

### Task 6: Environment cache

**Files:** Modify `tri/cli.py`; Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** tables `env_recipes(id, project_id, name, key_files_json, key_commands_json, install_json, outputs_json, mode, created_by, created_at)`, `env_builds(key, recipe_id, project_id, status, path, outputs_json, size_bytes, stdout_artifact, stderr_artifact, exit_status, error, last_used_at, created_at)`. `env_key(store, recipe, entries) -> str`; `ensure_env(store, recipe, entries, *, actor, workspace) -> sqlite3.Row` (per-key `FileLock`); `env_attachments(store, project_id, entries, *, actor, workspace, purpose) -> list[Attachment]`. Commands `tri env define|list|inspect|remove`. `tri run --no-env`, `tri workspace mount --no-env --env-link`. Env outputs skipped by scan, sync, salvage.

- [ ] Tests: `test_env_built_once_and_shared_across_workspaces`, `test_env_rebuilt_when_key_file_changes`, `test_env_outputs_not_imported`, `test_env_build_failure_fails_run_with_diagnostic`, `test_env_attached_to_mount_by_clone`.
- [ ] Implement.

### Task 7: Overlay backend (Linux runs)

**Files:** Create `tri/overlay.py`; Modify `tri/materialize.py`, `tri/cli.py`; Test `tests/test_bounded_disk.py` (skipped off Linux) and a Docker run.

**Interfaces — Produces:** `probe_overlay(home: Path) -> dict | None` (`{"userns": bool, "userxattr": bool}`); `OverlayRun` prepared by `Materializer.materialize(..., backend="overlay")` with `wrap_command(argv) -> list[str]` (an `unshare` + `mount -t overlay` shell wrapper) and `scan()` reading the upper dir (regular files, whiteouts `c 0 0`, opaque dirs).

- [ ] Tests (Linux only): `test_overlay_run_imports_edits_and_deletes`, `test_overlay_upper_holds_only_changes`.
- [ ] Verify inside Docker (`--privileged` and default seccomp) that probe chooses overlay or falls back.

### Task 8: GC and cache budgets

**Files:** Modify `tri/cli.py`; Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** blobs/trees columns `last_referenced_at`, `purged_at`; `gc_plan(store, *, retention_days, grace_seconds) -> dict`; `gc_apply(store, plan) -> dict`; `evict_caches(store) -> dict`; command `tri gc [--reap] [--apply] [--retention-days N] [--grace-seconds N]`; reads of purged content raise `content_purged`.

- [ ] Tests: `test_gc_dry_run_changes_nothing`, `test_gc_purges_discarded_workspace_content_after_retention`, `test_gc_keeps_live_and_recent`, `test_put_blob_after_purge_rewrites`, `test_cache_eviction_respects_budget`.
- [ ] Implement with a separate `FileLock(<home>/gc.lock)`.

### Task 9: Hierarchical trees

**Files:** Create `tri/trees.py`; Modify `Store.put_tree`, `Store.load_tree`, GC reachability; Test `tests/test_bounded_disk.py`

**Interfaces — Produces:** `HIER_PREFIX = "TRIH1:"`; `split_tree(entries) -> list[(dir_path, node_payload)]` bottom-up; node payload `{"d": {name: tree_id}, "f": {name: entry}}`; `Store.load_tree` flattens recursively with an in-process cache.

- [ ] Tests: `test_hier_tree_roundtrip`, `test_one_edit_adds_depth_nodes_only`, `test_flat_trees_still_load`.
- [ ] Full suite passes.

### Task 10: Fanout benchmark

**Files:** Modify `benchmarks/agentic.py`; Test `tests/test_benchmark_agentic.py`

**Interfaces — Produces:** scenario `fanout` (`--attempts`, `--source-files`, `--dep-files`), targets `tri` and `git`; metrics `peak_allocated_bytes`, `end_allocated_bytes`, `measure: "statvfs-volume" | "st_blocks"`, per-run `backend`. macOS measures on a sparse APFS image via `hdiutil`.

- [ ] Test: `test_fanout_scenario_small_runs` (tiny sizes, both targets ok).
- [ ] Run 10 and 50 attempts; record numbers in the report.

### Task 11: Docs

- [ ] README: new commands, backends table, behavior change (gitignored run outputs not imported), benchmark numbers.
