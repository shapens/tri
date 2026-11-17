# Tri

Tri is an agent-native source-control prototype based on [tri.md](tri.md).

This repository currently implements a local Python CLI and JSON HTTP API MVP. It stores content-addressed blobs/trees, auto-packed SQLite metadata, snapshots, workspace operations, events, check runs, changesets, reviews, artifacts, and export records under `.tri` or `TRI_HOME`.

## Competitive Baseline

Tri is not trying to beat Entire at session capture or GitButler at branch management.

- Entire's wedge is preserving prompts, transcripts, tool calls, file changes, and session metadata around Git commits.
- GitButler's wedge is making parallel/virtual and stacked Git branches workable, including hooks and MCP that record agent edits and turn them into branch commits.
- Tri's wedge is earlier: isolated workspaces, API-first mutations, pre-commit provenance, structured checks, object-level policy, secret leases, and reviewable changesets before Git or GitHub export.

Research refreshed 2026-05-28 from current public docs: <https://docs.entire.io/overview>, <https://docs.entire.io/cli/checkpoints>, <https://docs.entire.io/agents/overview>, <https://docs.entire.io/security>, <https://docs.gitbutler.com/overview>, <https://docs.gitbutler.com/features/branch-management/virtual-branches>, <https://docs.gitbutler.com/features/branch-management/stacked-branches>, <https://docs.gitbutler.com/features/coding-agents>, <https://docs.gitbutler.com/features/ai-integration/mcp-server>, and <https://docs.gitbutler.com/features/ai-integration/claude-code-hooks>.

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
- policy-gated changeset merge into Tri targets, with fast-forward, non-conflicting target merge, and conflict diagnostics
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

- hosted collaboration layer
- broader hosted Policy v0 language: visibility role mapping, embargo lifecycle, cross-object inheritance rules, and actor identity binding
- semantic conflict resolution beyond same-path ours/theirs/base choices
- remote materialization lease coordination beyond local lease records and stale-sync guards
- external secret-manager providers beyond the local secret-store stub
- semantic changeset splitting beyond path/range selectors and multi-node stack revalidation planning
- hosted merge/accept workflows
- live check streaming and remote runners beyond stored log chunks
- hosted GitHub/GitLab app integration beyond local `gh`/branch preparation
- package publishing, advisory backports, and hosted release workflows
