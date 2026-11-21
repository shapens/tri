# Tri

Tri is an agent-native source-control prototype based on [tri.md](tri.md).

This repository currently implements a local Python CLI and JSON HTTP API MVP. It stores content-addressed blobs/trees, auto-packed SQLite metadata, snapshots, workspace operations, events, check runs, changesets, reviews, artifacts, and export records under `.tri` or `TRI_HOME`.

## Competitive Baseline

The goal is to be the best agent-native source control. The field as of 2026-11-20:

- Forges (Entire, Cursor Origin, GitHub, GitLab) host Git and attach agent context or agent scores to commits and pull requests. GitButler makes parallel and stacked Git branches workable and ships an MCP server. All of them still use Git as the live record.
- Anthropic's internal Scion replaced Git for one agent-scale monorepo: server-side changesets, no full clones, server-side blame and merge bases, no global merge lock. Its access control is the thin part of its public write-up.
- Tri's wedge is the live record itself: isolated workspaces, API-first mutations, pre-commit provenance, structured checks, object-level policy, secret leases, and reviewable changesets before any Git or GitHub export.

`tri.md` holds the full comparison, the gaps that remain (replicated storage behind the service, virtual checkouts, a remote runner pool), and the scorecard Tri has to win.

The marginal advantage to prove is small and specific: an agent should be able to mutate, check, review, protect, and export work through stable Tri objects without first editing a shared working directory, assigning changes to a branch, or treating a GitHub pull request as the live coordination surface.

## Run

Install the local CLI in editable mode once:

```sh
python3 -m pip install -e .
```

Then use `tri` directly:

```sh
tri --help
```

Use an isolated store while testing:

```sh
TRI_HOME=/tmp/tri-home tri import ./repo --name app
```

Run the local agent API:

```sh
TRI_HOME=/tmp/tri-home tri serve --host 127.0.0.1 --port 8765
curl -s http://127.0.0.1:8765/v0/commands
curl -s -X POST http://127.0.0.1:8765/v0/command \
  -H 'Content-Type: application/json' \
  -d '{"actor":"agent:a","argv":["workspace","list"]}'
```

Anything beyond loopback needs bearer tokens, each bound to one actor:

```sh
TRI_HOME=/tmp/tri-home tri auth issue agent:a --file /tmp/tri-tokens.json   # prints the token once
TRI_HOME=/tmp/tri-home tri serve --host 0.0.0.0 --port 8765 --auth-file /tmp/tri-tokens.json
curl -s -X POST http://host:8765/v0/command -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"argv":["workspace","list"]}'
```

Connect an agent harness over MCP (stdio). Every tool call runs as the actor the server starts with:

```sh
claude mcp add tri -- env TRI_HOME=/tmp/tri-home tri --actor agent:claude mcp
```

Point clients at one central server instead of a local store. The server is the only copy; each machine keeps a blob cache and its mounted files:

```sh
export TRI_REMOTE=https://tri.example.com TRI_TOKEN=tri_...      # or TRI_TOKEN_FILE
tri workspace create app --from main                          # runs on the server
tri workspace mount ws_... ./wt --paths services/api          # fetches only uncached content
tri workspace sync ws_...                                      # one batched upload of what changed
tri run ws_... -- make test                                    # executes on the server, sandboxed
claude mcp add tri -- env TRI_REMOTE=https://tri.example.com TRI_TOKEN_FILE=~/.tri-token tri mcp
```

Beyond loopback, `tri serve` sandboxes runs and refuses API arguments that name server paths (`files write --body`, `import <dir>`, `workspace mount <dir>`, ...) unless the caller is a maintainer.

## Implemented MVP Surface

- machine-readable command discovery, schema catalog, response links for visible refs, generic typed-object inspection, derived actor identity inspection, actor-aware package boundary declaration/inspection, project discovery/inspection, and store doctor
- local JSON HTTP API with health, command discovery, schema lookup, command execution, and dependent batch execution endpoints
- local `tri batch` JSON plans for dependent agent command sequences with prior-result placeholders, explicit returned values, and compact summary responses
- embedded Python batch execution for agents and benchmark harnesses that integrate Tri as a source-control primitive instead of shelling out
- auto-packed SQLite metadata that reopens transparently between commands to keep small agent workspaces cheap on disk without forcing compression into larger hot paths
- Git import into Tri projects, targets, blobs, trees, and snapshots, with Git commit/tree source mapping and author actor references exposed in the response
- policy-aware blob/tree inspection that exposes visible origins and file read routes without allowing raw content reads by guessed content id
- symlink target preservation on import without following symlink targets during tree hashing, plus safe symlink materialization that rejects escaping targets
- target listing, direct `target_...` inspection, and refresh from the imported project root with explicit expected-snapshot and internal compare-and-swap protection
- workspace creation from imported targets
- workspace list, inspect, explicit snapshot, pause/resume, discard, archive, active/read-only mount, inspectable materialization leases, and sync with stale-materialization compare-and-swap protection
- generic lease inspection for either materialization or secret leases by `lease_...` id
- workspace checkpoint, base update, and conflict resolution with stale-head compare-and-swap protection
- workspace base update with persisted same-path conflict objects, direct conflict listing/inspection URLs, and explicit resolve strategies
- file API: list/stat/read/search against workspaces and snapshots, plus write/delete/move against workspaces
- project search across authorized targets, workspaces, changesets, tasks, reviews, checks, releases, check logs, release artifacts, and workspace history with provenance
- automatic snapshots, operations, and events for workspace mutations
- workspace mutation and command events record policy decisions, and changesets expose redacted operation details with policy provenance
- optimistic stale-head detection for file API mutations and command-run file imports
- workspace diff from base snapshot to head snapshot
- local command execution as structured check runs with actor, cost, materialized-directory fingerprint, and environment-policy provenance
- check logs, workspace/changeset check listing, offset-addressed stored log streaming, generated artifacts, artifact listing, reruns, comparison, and reusable required-check sets with direct `checkset_...` inspection
- executable task specs with body, acceptance criteria, inline or named-set required checks, task-run inspection with commands/checks/artifacts/resulting changesets/cost, final recommendations, and task lifecycle commands for block/resume/accept/reject/close
- changeset creation from workspace operations
- task attempt listing with policy-redacted private attempt refs, and changeset comparison for competing agents
- path and operation-range split preview
- changeset discard/supersede lifecycle commands with replacement metadata and audit-readable retired changesets
- changeset dependency stacking with merge/export gating and head-snapshot check revalidation
- policy-gated changeset merge into Tri targets, with fast-forward, non-conflicting target merge, line-level three-way merges for text files edited on both sides, compare-and-swap retries under concurrent merges, and conflict diagnostics
- maintainer merge rules that let checked changesets touching only matching paths merge without a human review, while every other merge gate still applies
- history queries without a checkout: path log, line blame with snapshot/actor/changeset provenance, and snapshot merge bases, all stopping at snapshots the actor cannot read
- bearer-token actors for `tri serve` (`tri auth issue|list|revoke`, `--auth-file`), with actor pinning across HTTP routes and batches and a refusal to serve beyond loopback without tokens
- MCP stdio server (`tri mcp`) exposing `tri` and `tri_batch` tools pinned to one actor
- remote mode (`TRI_REMOTE`, `TRI_TOKEN`): the CLI and `tri mcp` run every command on one central `tri serve`; `workspace mount`/`sync` work through a per-machine blob cache (`TRI_CACHE_DIR`), and `files fetch` reads many files per round trip
- post-merge verification: merge rules with `--verify-after <check-set>` merge first; `tri verify run` checks merged snapshots in order and reverts failures on the current target, keeping later merges
- sandboxed runs (`tri run --sandbox`, `tri serve --run-sandbox`, default `required` beyond loopback): bubblewrap on Linux, the system sandbox on macOS; no network, no writes outside the run directory, no reads of the Tri home or credential files, scrubbed environment, CPU/memory/file-size/wall-clock limits
- partial checkouts for large trees (`tri run --paths <glob>`, `tri workspace mount --paths <glob>`), with run directories deleted in the background and new base directories derived from the previous base
- two-way Git mirror (`tri mirror add|sync|inspect|list|remove`): Git pushes land as attributed snapshots, Tri merges land as Git commits titled by changeset, both-sides edits line-merge into a merge commit, and private packages and secret-like paths never leave Tri
- review object creation and inspection with task context, changeset summary, checks, artifacts, policy findings, attached policy/check evidence, visibility impact, reviewer state, threaded comments, and export preview
- anchored review comments, direct review-action inspection, replies, thread resolution, requested changes, assigned-reviewer approvals, risk markers, explicit policy-finding/check-summary attachments, explicit review close, and request-agent-pass workspaces
- Policy v0 export evaluation requiring passing checks and approved review, with explicit local override records
- workspace lifecycle policy that blocks discarded-workspace changesets from merge/release/export
- local Policy v0 actor/role grants, grant inspection/revocation, workspace-to-changeset/review read inheritance, private workspace/changeset enforcement, policy-gated access/visibility explanation, explainable policy-denied diagnostics, and embargo create/disclose commands
- path-level hidden globs that redact file listing/search/diff/review surfaces and block export
- policy-gated secret references with direct HTTP inspection, separate `secret.read` and `secret.use` grants, workspace-scoped secret leases, lease audit/revoke, pre-storage log redaction, and explicit check-log redaction repair
- advisory records with embargoed workspace/changeset defaults, export/merge blocking, and release-prepare disclosure
- policy-gated event listing/inspection with direct event URLs, secret audit, and artifact listing/inspection/reading
- generated config artifact with `non-exportable` label, materialization-lease linkage, and export blocking
- Git binary-safe patch export with secret-like path blocking
- Git commit export into a clean target repository
- GitHub PR export adapter that prepares a branch and PR request artifact, with optional `gh pr create`
- successful exports advance the changeset lifecycle to `exported` while keeping repeated export records inspectable
- export record listing/inspection with policy decisions, exported object ids, source event ranges, and re-export determinism metadata

## Bounded-Disk Workspaces

Git worktrees make every attempt a full checkout, grow their own `node_modules`/`.venv`/build output, and stay on disk until someone removes them. Tri keeps disk use proportional to the directories in use *right now*:

- **An attempt is a database row.** `tri workspace create` writes no files. Agents edit through the file API; directories exist only while a command runs (`tri run`) or a mount is leased.
- **Directories cost what they change.** Each machine is probed for the cheapest safe way to lay out a tree:

  | Backend | Where | How |
  | --- | --- | --- |
  | `clone` | APFS (macOS), btrfs / XFS / bcachefs / ZFS (Linux `FICLONE`), ReFS / Dev Drive (Windows) | Copy-on-write clones from a raw checkout cache; on macOS a whole base tree is cloned in one call and only the workspace's delta is applied |
  | `overlay` | Linux runs where unprivileged user + mount namespaces are allowed (default there; Ubuntu 24.04 needs `kernel.apparmor_restrict_unprivileged_userns=0`) | The run's directory is an overlay mount that exists only inside the command's namespace, and the command runs as the caller; only written files land on disk |
  | `copy` | Anywhere else (ext4, NTFS, cross-volume mounts) | Plain files, reported as `degraded` by `tri doctor`, checks, and leases |

  Override with `TRI_MATERIALIZE_BACKEND=auto|clone|copy|overlay`. Change detection compares a stat baseline (`size`, `mtime`, inode, mode), so a run only reads files it touched. Materialized files carry the fixed mtime 2000-01-01.
- **Dependencies exist once per lockfile.** `tri env define app deps --key-file package-lock.json --install "npm ci" --output node_modules` builds `node_modules` once per key (key files + key commands + platform) under a per-key lock, freezes it read-only, and attaches it to every run and mount by clone; on `copy` volumes a run gets a private copy (tools write into `target/` and `node_modules/.cache`) and a mount gets it only with `--env-link` (a read-only symlink). Overlay runs use a writable twin of the env as a lower layer. `--mode copy|link|private` overrides. Env outputs are never imported into snapshots.
- **Every directory has an expiring owner.** Runs hold a lease and a holder lock; a crashed run's directory is salvaged and removed by the reaper. Mounts default to a 24-hour TTL (`--no-ttl` to opt out; `tri workspace sync` and `tri leases renew` extend it). Before anything is deleted, unsynced edits are captured as a **salvage snapshot** (`tri salvage list`, `tri salvage adopt`); only ignored dependency/build directories (`node_modules`, `.venv`, `target`, `dist`, `__pycache__`, … plus `TRI_DERIVED_DIRS`) and OS junk files are deleted without capture; a directory holding secret-like files, other ignored files, or a hand-made `.git` waits in `expired_needs_attention` until `tri leases reap <lease> --force`. One live lease owns a directory: mounting onto a path another live lease owns is refused (`mount_path_in_use`), and a directory that was deleted and recreated by someone else is never reaped. Mounts created before this change are grandfathered and never auto-reaped. The reaper runs after CLI commands (at most every 10 minutes; `TRI_AUTO_REAP=0` disables), every 5 minutes inside `tri serve`, and on demand with `tri leases reap`.
- **Old data is reclaimable.** `tri gc` plans; `tri gc --reap` reaps leases and trims caches to budget (`TRI_CHECKOUT_CACHE_MAX_BYTES`, `TRI_BASE_CACHE_MAX_COUNT`, `TRI_ENV_CACHE_MAX_BYTES`); `tri gc --apply` also purges content no live object references once retired work passes the retention window (14 days; `--retention-days`, `--grace-seconds`, minimum 60 s unless `--unsafe-no-grace`); `tri gc --clear-caches` drops every cache nothing live uses. Metadata and events are kept; reading purged content returns `content_purged`.
- **History grows with the size of a change.** Snapshot trees are stored as one content-addressed node per directory, so an edit writes only the nodes on its path.

**Behavior change:** runs, syncs, and salvage no longer import *new* files matched by the snapshot's `.gitignore` files or by `tri ignore add <project> <glob>` patterns (build output, caches). Tracked files are always imported, even under ignored directories.

### Measured (macOS, APFS, `tri-bench-fanout`)

Each attempt edits two files, needs a dependency tree, and runs a check. Space is the dedicated APFS volume's own used figure, so shared clone blocks count once. `tri` = API edits + ephemeral runs; `tri-mount` = additionally keeps one persistent mounted directory per attempt (the closest analogue of a worktree); `git` = one worktree per attempt with dependencies installed in each.

| Fixture | Attempts | `tri` | `tri-mount` | git worktrees |
| --- | ---: | ---: | ---: | ---: |
| 2,000 source + 5,000 dependency files | 10 | 60.6 MiB | 83.2 MiB | 325.5 MiB |
| 1,000 source + 2,000 dependency files | 10 | 28.7 MiB | 38.4 MiB | 141.8 MiB |
| 1,000 source + 2,000 dependency files | 50 | 30.4 MiB | 79.1 MiB | 672.6 MiB |
| Same fixture: growth per extra attempt (10→50) | | ~43 KiB | ~1.0 MiB | ~13.3 MiB |

Raw results: `benchmark-results/fanout-10`, `fanout-10-small`, `fanout-50`. Fifty Tri attempts use about 1.4× the space of a single git worktree with its dependencies; fifty worktrees use about 30×. A persistent mount costs per-file metadata for its cloned tree (~1 MiB per 3,000 files), not file contents.

With 8 attempts running at once (20 attempts, same small fixture; `benchmark-results/fanout-20-parallel8`):

| Target | Peak | End |
| --- | ---: | ---: |
| `tri` (clone) | 36.0 MiB | 29.1 MiB |
| `tri` forced to `copy` | 59.0 MiB | 23.9 MiB |
| `tri-mount` | 53.0 MiB | 49.4 MiB |
| `tri-mount` with `--env-link` | 41.7 MiB | 37.3 MiB |
| git worktrees | 275.0 MiB | 274.1 MiB |

The `copy` backend peaks with the number of live runs and drops back when they finish; it never grows with attempt count.

### Real project: a Rust workspace

`tri-bench-fanout --repo <rust-workspace> --install "cargo test -p example-crate --no-run" --check "cargo test -p example-crate" --key-file Cargo.lock --key-file rust-toolchain.toml --env-output target --edit-file example-crate/src/lib.rs` (2 attempts each; `benchmark-results/rust-workspace-probe`):

| | Tri | git worktrees |
| --- | ---: | ---: |
| Disk at the end | 1.0 GiB (one shared `target/` + store) | 2.8 GiB (1.4 GiB per worktree) |
| Median time per attempt (edit + build + test) | 44 s | 127 s |

What the real project showed:

- **A shared `target/` works as a warm start.** Cargo validates fingerprints, so keying the environment on `Cargo.lock` + `rust-toolchain.toml` is enough; source edits only rebuild the crates they touch. The fixed 2000-01-01 mtimes did not trigger rebuilds.
- **Crates that `include_str!`/`include_bytes!` files from outside their own directory rebuild on every run.** Cargo records those paths absolutely, and each run lives at a different path. In `example-crate` that costs about 24 s per run; dependencies are unaffected. A stable per-run path (possible with Linux mount namespaces) would remove it.
- **Shared environments are frozen read-only**, so delete a Tri home with `tri gc --clear-caches` first (or `chmod -R u+w` it); plain `rm -rf` fails on frozen files.

## Verify

```sh
python3 -m py_compile tri/cli.py
python3 -m unittest discover -s tests -v
```

## Benchmarks

Run the agentic source-control workflow benchmark:

```sh
tri-bench
```

Run the small workflow smoke scenario:

```sh
tri-bench --scenario small
```

Run the full repeated workflow benchmark:

```sh
tri-bench --scenario full
```

Run the scaled stress workflow with batched Git and verifier outcomes:

```sh
tri-bench --scenario stress
```

Run the DeepSWE-style Tri task verifiers:

```sh
tri-bench-tasks
```

The workflow benchmark writes `results.json` and `summary.md` under `benchmark-results/<timestamp>/`. It compares Tri, Git worktree, SVN, GitButler, and Entire targets when the required CLIs are installed, with optional `tri-cli` and `tri-http` targets for comparing Tri's batch primitive over subprocess and local HTTP transports.

Run the fan-out disk benchmark (N attempts that each edit, install dependencies, and run a check; Tri versus git worktrees, measured on dedicated APFS volumes on macOS):

```sh
tri-bench-fanout --attempts 10
```

The task benchmark uses `benchmarks/tasks/<task-id>/` packages with `task.toml`, `instruction.md`, `environment/`, `tests/`, and `solution/`. These task verifiers complement the workflow benchmark by checking observable Tri behavior such as workspace isolation, hidden-path redaction, and conflict resolution. See [benchmarks/README.md](benchmarks/README.md).

## Example

```sh
tri import ./repo --name app
tri actors inspect agent:a
tri packages declare app frontend --path "src/**" --visibility public
tri packages declare app internal --path "internal/**" --visibility private --owner role:maintainer
tri checks set-create app smoke --required-check "npm test"
tri task create app --title "fix failing tests" --acceptance "regression test passes" --check-set smoke
tri workspace create app --from main --task task_... --hide-path "private/**"
tri objects inspect ws_...
tri files search ws_... "checkout"
tri search app "checkout" --include-history
tri files stat ws_... src/app.ts
tri files write ws_... src/app.ts --body ./candidate.ts
tri files move ws_... src/old.ts src/new.ts --expect-head snap_...
tri files delete ws_... src/dead.ts --expect-head snap_...
tri diff ws_...
tri diff snap_base... snap_head...
tri workspace pause ws_...
tri workspace resume ws_...
tri workspace mount ws_... ./mounted --ttl-seconds 3600
tri workspace leases ws_...
tri leases inspect lease_...
tri workspace sync ws_... --lease lease_...
tri workspace snapshot ws_... --expect-head snap_...
tri workspace update ws_... --from main --expect-head snap_...
tri conflicts list --workspace ws_...
tri conflicts resolve conflict_... --strategy theirs
tri env define app deps --key-file package-lock.json --install "npm ci" --output node_modules
tri ignore add app "coverage/**"
tri leases list --workspace ws_...
tri leases renew lease_... --ttl-seconds 86400
tri leases reap
tri salvage list ws_...
tri salvage adopt salvage_...
tri gc --reap
tri gc --apply
tri secrets put app API_TOKEN --value-file ./token.txt
tri secrets inspect secret_...
tri secrets request-lease ws_... API_TOKEN --env API_TOKEN
tri config materialize ws_... --profile test
tri run ws_... --with-lease lease_... -- npm test
tri secrets redact-logs check_... --lease lease_...
tri checks list ws_...
tri checks list cs_...
tri checks inspect check_...
tri checks stream check_... --log stdout --offset 0 --limit 4096
tri task run-inspect run_...
tri task complete-run run_... --recommendation "Use this changeset."
tri task accept task_... --changeset cs_...
tri task close task_... --reason "shipped"
tri changeset create ws_... --title "Fix failing tests"
tri changeset inspect cs_...
tri changeset split cs_... --operations "1..2"
tri changeset stack cs_child... --after cs_parent...
tri changeset revalidate cs_child...
tri changeset ready cs_... --reason "addressed review"
tri changeset supersede cs_old... --by cs_new...
tri changeset discard cs_bad... --reason "wrong approach"
tri policy evaluate access cs_... --action changeset.read
tri policy explain decision_...
tri policy create-embargo app --title "Auth bypass"
tri policy disclose-embargo app --include cs_... --advisory adv_...
tri policy visibility cs_...
tri policy grant cs_... --principal actor:agent:b --action changeset.read
tri policy grant-inspect grant_...
tri review create cs_...
tri review comment review_... --path src/app.ts --line 12 --body "why this branch?"
tri review action-inspect revact_...
tri review resolve-thread review_... --thread revact_...
tri review attach-check-summary review_...
tri review attach-policy-finding review_...
tri review close review_... --body "superseded by another attempt"
tri review approve review_...
tri advisory create app --title "Auth bypass" --embargoed
tri workspace create app --from main --advisory adv_...
tri release prepare app --include cs_... --disclose adv_...
tri changeset merge cs_... --target main
tri policy evaluate export cs_...
tri review request-agent-pass review_... --task "minimize the patch"
tri export git-patch cs_... --output ./change.patch
tri export github-pr cs_... --repo ./repo --branch tri/fix-tests --base main
tri export inspect export_...
tri export list app --changeset cs_...
```

## Remaining Gates

- verify ReFS/Dev Drive clones on a Windows machine that has them (probe-guarded; falls back to `copy`). Overlay, XFS/btrfs reflinks, and the Windows port are verified in CI.

- hosted collaboration layer: server-authoritative storage with disks as caches, sparse checkouts, and tiered caches (see "Agent-Scale Gaps" in `tri.md`)
- sandboxed remote runners before untrusted agents can call `run` on a shared server
- post-merge verification lane with automatic revert for low-risk paths
- broader hosted Policy v0 language: visibility role mapping, embargo lifecycle, cross-object inheritance rules, and actor identity binding
- semantic conflict resolution beyond same-path ours/theirs/base choices
- remote materialization lease coordination beyond local lease records and stale-sync guards
- external secret-manager providers beyond the local secret-store stub
- semantic changeset splitting beyond path/range selectors and multi-node stack revalidation planning
- hosted merge/accept workflows
- live check streaming and remote runners beyond stored log chunks
- hosted GitHub/GitLab app integration beyond local `gh`/branch preparation
- package publishing, advisory backports, and hosted release workflows
