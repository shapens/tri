# Tri

Tri is agent-native source control. The goal is to be the best one.

"Best" is measured, not claimed. Tri has to beat Git forges on the work agents actually do, and it has to beat Anthropic's Scion, the one system that has already replaced Git for an agent-scale monorepo, on everything except raw scale on day one. The scorecard is in [Agent-Scale Scorecard](#agent-scale-scorecard).

It gives humans and agents durable workspaces, automatic snapshots, reviewable changesets, structured checks, scoped secrets, object-level visibility, and provenance without making Git branches, Git worktrees, or GitHub pull requests the center of the workflow.

Git and GitHub can remain compatibility targets. They should not define the live model.

## Thesis

Git is a strong archival and interchange format, but it is the wrong live workspace model for agents.

Agents should not have to:

- create branches before trying work
- stage files manually
- decide when a commit exists
- manage worktrees
- scrape CI logs
- copy `.env` files
- infer review context from a pull request page
- pretend a local filesystem checkout is the source of truth

Tri replaces the live model with a workspace database:

```text
project -> snapshots -> workspaces -> operations -> changesets -> reviews -> exports
```

The source of truth is immutable snapshots plus an append-only event log. A local checkout, filesystem mount, Git branch, or GitHub PR is an adapter.

## Design Target

The first useful Tri should make this workflow boring:

1. Import an existing Git repo.
2. Create three isolated agent workspaces from the same base.
3. Let each agent read and write files through an API.
4. Snapshot every mutation and command boundary automatically.
5. Attach commands, checks, logs, artifacts, assumptions, and costs to the task run.
6. Turn each workspace into one or more reviewable changesets.
7. Review, split, merge, discard, or export those changesets.
8. Export accepted work back to Git as a patch, commit, or pull request.

If Tri is not clearly better than `git worktree` plus GitHub PRs for this flow, it is not worth continuing.

## Non-Goals

Tri should avoid becoming too broad too early.

Not required for the first proof:

- replacing every Git remote
- replacing every GitHub issue feature
- replacing package registries
- building a full hosted social coding network
- inventing a programming language
- perfect semantic diffs
- decentralized sync
- production-scale CI orchestration
- a visual review UI

Also not acceptable:

- being only a nicer Git wrapper
- treating Git branches as the internal unit of parallelism
- treating commits as the default authoring primitive
- treating repo-level public/private as the only visibility model
- requiring agents to use a shell when a structured API can do the job

## Competitive Baseline

Tri should learn from the field instead of pretending it does not exist.

Research refreshed: 2026-11-20. The field split in two during 2026.

1. **Faster Git forges with agent review.** Entire, Cursor Origin, GitHub, and GitLab all host Git repositories and attach agent context or agent scores to commits and pull requests. GitButler is a Git client. Devin, Codex, Copilot, and Claude Code on the web finish by pushing a branch and opening a pull request on one of them. None of them changes the live record: a clone can still read every blob, and the pull request is still the decision surface.
2. **Server-side source control for one giant repository.** Anthropic's Scion replaced Git for Anthropic's monorepo. It is internal, and its prior art is Google's Piper and Meta's Sapling/Mononoke. It proves the live model can leave Git, but it optimizes for scale, not for policy.

Nobody makes the workspace, the changeset, and the policy decision the source of truth with Git only as an export. That is Tri's lane. The adoption risk is that teams decide a faster Git forge is enough.

| Product | What it is now | Still Git? | What Tri takes from it |
| --- | --- | --- | --- |
| [Scion](https://www.anthropic.com/research/redesigning-source-control) | Anthropic's internal replacement for Git, live since October 2026 | No | Server-authoritative storage, no full clones, server-side queries, no global merge lock |
| [Entire](https://entire.io/) | Forge from the former GitHub CEO, opened 26 October 2026; Trails replace the pull request with agent scores on each push | Yes | Agent context belongs next to the change, not in a separate tool |
| [Cursor Origin](https://cursor.com/docs/origin) | Cursor's forge, early beta since 17 August 2026, with GitHub sync | Yes | Two-way GitHub sync is the adoption path |
| GitHub, GitLab | The default destination; Copilot cloud agent, Agent HQ, Duo agents | Yes | Everyone already lives here; export must stay boring |
| GitButler | Local client for parallel and stacked branches, with `but mcp` | Yes | MCP is how agents reach source control |
| Zed DeltaDB | Stores every edit and agent message between commits; teammates without Delta see plain Git | Partly | Keep a normal Git view for people who never install Tri |

The honest claim is still narrow: Tri wins for agents only when the live source-control primitive is the workspace, the changeset, and the policy decision, not a Git commit, branch, or pull request.

### Scion

Source: <https://www.anthropic.com/research/redesigning-source-control> (13 October 2026, updated 15 October). No code, protocol, or client is public. Anthropic says it is "by no means a suitable replacement for git for general use."

Why Anthropic left Git: merges grew from about 2,500 a day in May 2026 to more than 30,000 in September. Main-branch history is almost 100 GB and CI did on the order of 25 million checkouts a day. Offline, complete clones were the part that hurt; nearly every client was an always-online agent or job.

How Scion works:

- the unit of work is a changeset: a write-ahead-log entry with the files changed against its first parent and content-addressed pointers to their contents
- changesets are created on the server and visible everywhere at once; there is nothing to push
- blame, path history, and the commit graph are derived data the server computes and can re-index without client changes
- there is no packfile and no fully materialized copy, even on the server; Spanner and cloud storage hold the truth and servers are caches
- the `scn` client keeps a bounded object cache and lays out sparse or virtualized checkouts; jobs reach `scion-server` through `scion-daemon` and a regional `scion-cache`, each tier serving the same RPCs
- moving a ref is a compare-and-swap on one row, separate from creating the changeset
- many CI jobs never check out files; they ask the server for a diff, a merge base, or blame
- there is no global merge-queue lock; tests run after merge and before deploy, and Claude usually fixes concurrent-change conflicts before a person notices

What Scion leaves thin is exactly Tri's strength. The post describes repository access control, per-directory permissions, and approval rules that differ for a comment-only change and a sensitive logic change. It does not describe actors, grants, secret handling, embargoes, or what a denied read returns.

Tri's position against Scion:

- Tri already matches the shape: content-addressed snapshots, changesets created against a base, refs moved by compare-and-swap, disk as a cache.
- Tri must close the scale gap: server-authoritative storage, sparse checkouts, tiered caches, and server-side history queries. See [Agent-Scale Gaps](#agent-scale-gaps).
- Tri must keep its lead on policy: object-level visibility, secret leases, embargoed advisories, and machine-readable denials are the things Scion does not claim.

### Entire

Update 2026-11: Entire is now a forge. It opened on 26 October 2026, hosts Git repositories, replaces the pull request with Trails (agent scores on each push), and keeps agent session memory. The checkpoint analysis below still describes how its context capture works; the difference is that Entire now also competes for the hosted destination, so a hosted Tri would compete with it directly.

### Entire

Sources: <https://entire.io/>, <https://entire.io/blog/hello-entire-world>, <https://docs.entire.io/overview>, <https://docs.entire.io/cli/checkpoints>, <https://docs.entire.io/agents/overview>, <https://docs.entire.io/web/inspect-session>, <https://docs.entire.io/security>, <https://github.com/entireio/cli>

Entire's strong idea is that agent work loses too much context when only the Git diff survives. It captures prompts, transcripts, tool calls, files touched, token usage, and session metadata, then links that context to Git commits through checkpoints.

Entire is strong for:

- preserving why a commit happened
- making prior agent sessions searchable
- resuming from earlier agent context
- adding audit context without dirtying normal commit history
- working inside existing Git workflows
- supporting many agent integrations through hooks, plugins, and agent-specific adapters
- showing checkpoint/session detail in a web UI with transcripts, tool calls, diffs, token usage, sub-agent sessions, and AI attribution

Entire's current center of gravity is still Git:

- checkpoints are created around Git commits
- checkpoint IDs are added to commit trailers
- metadata is stored on a Git branch
- normal source control remains branches, commits, pushes, and worktrees
- concurrent sessions are tracked, but they do not become isolated source-control workspaces with first-class changesets
- agent integrations observe and preserve work around the Git workflow; they do not replace source-control mutation with object APIs

Evidence checked: Entire's docs describe checkpoints as permanent records created when `git commit` runs, session data attached to the commit, metadata stored in `entire/checkpoints/v1`, and the checkpoint branch pushed with normal Git push. The web docs describe sessions as complete records of prompts, responses, tool calls, file changes, token usage, sub-agent sessions, and attribution, with every session linked to one or more Git commits through the checkpoint trailer. The agent docs describe lifecycle hooks for Claude Code, Codex, Copilot CLI, Cursor, Factory Droid, Gemini CLI, OpenCode, Pi, and external plugins that capture sessions and link them to Git history.

Important security lesson: Entire's redaction is explicit and serious, but it is still a safety layer over captured transcripts and Git storage. Its docs say checkpoint metadata lives on a Git branch that anyone with repo access can read, public repos make that branch public, redaction is best-effort, commit trailers are public forever, and temporary shadow branches can contain raw source-file snapshots. Tri should not rely on redaction as the primary protection for source-control privacy.

Tri's marginal edge over Entire is not better transcript capture. Entire is already good at that. Tri is only better for agents if it moves the context boundary earlier and makes the mutation path itself become the source-control record:

- before commit, not after commit
- at every mutation and command boundary, not only checkpoint moments
- inside an isolated workspace, not the active Git branch
- attached to changesets, checks, policies, and reviews, not just commits
- protected by object-level visibility, not only a private checkpoint branch
- evaluated before export, not only explained after Git history exists

That means Tri should not try to out-record Entire. Tri should record enough task, run, check, policy, and review context for source-control decisions before a commit exists, then allow richer external transcripts to be attached as artifacts if they are useful.

### GitButler

Sources: <https://gitbutler.com/>, <https://github.com/gitbutlerapp/gitbutler>, <https://docs.gitbutler.com/overview>, <https://docs.gitbutler.com/features/branch-management/virtual-branches>, <https://docs.gitbutler.com/features/branch-management/stacked-branches>, <https://docs.gitbutler.com/features/coding-agents>, <https://docs.gitbutler.com/features/ai-integration/ai-overview>, <https://docs.gitbutler.com/features/ai-integration/mcp-server>, <https://docs.gitbutler.com/features/ai-integration/claude-code-hooks>, <https://docs.gitbutler.com/features/ai-integration/cursor-hooks>, <https://docs.gitbutler.com/features/timeline>, <https://docs.gitbutler.com/cli-overview>

GitButler's strong idea is that Git branch juggling is the wrong human interface. It lets users work with parallel and stacked branches in one working directory, assign changes to lanes, keep operation history, integrate with GitHub/GitLab PRs, and run coding agents attached to branches.

GitButler is strong for:

- parallel/virtual branches in one working directory
- stacked branches and stacked pull requests
- moving changes between branches
- automatic snapshots before major operations
- easier commit editing
- first-class conflict handling
- GitHub/GitLab PR integration
- branch-attached Claude Code sessions
- agent hooks and MCP integration for recording prompts/changes and updating branches
- automatic commit/save-point behavior from agent hooks or MCP calls
- automatic branch, commit, and PR description assistance

GitButler's current center of gravity is still Git branches and commits:

- parallel work is represented as parallel/virtual or stacked branches
- each lane has branch-like staging and commit behavior
- agent sessions are tied to branches
- agent integrations tell GitButler how to record and organize branch output around filesystem edits
- upstream collaboration still exports through PRs
- the working directory is still the main live editing surface
- Git remains the underlying storage and conceptual boundary

Evidence checked: GitButler's docs describe parallel branches as simultaneous branch lanes in one working directory, stacked branches as dependent branches with stacked pull requests, branch-attached Claude Code sessions, operation-history snapshots stored in the Git object database, Claude/Cursor hooks that assign edits and commit branch output, and an MCP server through `but mcp`. The current MCP overlap is especially important: the documented tool is `gitbutler_update_branches`, where an agent sends the prompt and resulting filesystem changes to GitButler, GitButler records them quickly, and GitButler then processes the raw recorded change into branch commits. The docs describe future MCP work such as auto-absorbing changes into existing commits, creating branches from prompt themes, and creating stacked branches. Claude Code hooks run before and after edit tools and on stop so GitButler can assign edits to the right branch and commit them.

Tri's marginal edge over GitButler is not a better branch UI. GitButler is already good at branch UI. Tri is only better for agents if it avoids turning "branch management for humans" into "branch management for agents." Agents need isolation, APIs, stale-write protection, review evidence, and policy more than they need a better branch lane.

### What Tri Must Do Differently

Tri should not compete by being a nicer Git client or a better session recorder. Those are already good products. If Tri only exposes Git through MCP, GitButler is closer. If Tri only records prompts and diffs, Entire is closer. Tri wins only if agents can use it as source control before Git exists in the workflow.

Tri's agent-specific wedge:

- **Workspace isolation:** each agent gets a durable copy-on-write workspace that can be mounted or discarded; it is not sharing one working directory lane.
- **API-first mutation:** agents read, write, diff, search, and run checks through structured APIs instead of using Git porcelain or GUI state.
- **Pre-commit provenance:** every file mutation, command boundary, check, secret lease, and export is recorded before any Git commit exists.
- **Changeset review:** the review unit is a changeset with selected operations, checks, risks, visibility, and provenance, not a branch or commit.
- **Object-level policy:** visibility and export rules apply to files, paths, workspaces, changesets, reviews, logs, artifacts, packages, and advisories.
- **Secrets by construction:** secrets are leases and generated config artifacts with non-exportable policy, not values captured and later redacted.
- **Structured checks:** command results are typed check-run objects, not CI log text or session transcript evidence.
- **Competing attempts:** a task can spawn multiple agent workspaces and compare their changesets directly.
- **Agent contract:** commands, schemas, diagnostics, events, artifacts, and review objects are stable machine-readable interfaces, not UI state or prose conventions.

Tri should concede the places where it is not better:

- If the question is "why did this commit happen?", Entire is closer to the current need.
- If the question is "how do I split local work into Git branch lanes or stacked PRs?", GitButler is closer to the current need.
- If a team wants GitHub pull requests to remain the source of truth, Tri should integrate with that workflow as an adapter, not imitate every GitHub screen.

The "marginally better" test is intentionally narrow:

- Compared with Entire, Tri must let an agent create, inspect, review, and protect useful work before a commit or checkpoint exists.
- Compared with GitButler, Tri must let multiple agents work from the same base without sharing one live working directory or requiring branch assignment as the coordination primitive.
- Compared with both, Tri must make policy decisions machine-readable before export, including private work, hidden paths, secret-bearing artifacts, advisory embargoes, and required checks.
- Compared with both, Tri must keep Git and GitHub as adapters: useful outputs, but not the live state machine agents have to reason about.

The practical margin is earlier object creation and earlier policy enforcement. Entire and GitButler can observe or organize agent output after filesystem edits exist. Tri should make the source-control operation itself be the thing the agent calls: `files.write`, `run`, `changeset.create`, `review.comment`, `policy.evaluate`, and `export.github-pr` all return typed objects, refs, diagnostics, and events before the work is translated into Git or GitHub.

The closest GitButler overlap is useful pressure for Tri's design. If an agent edits a shared working directory and then calls a branch updater, Tri is probably not differentiated. If the agent calls Tri to perform the edit, receives a new snapshot, operation, event, diagnostics, and policy context in one response, and never has to assign the change to a branch until export, then Tri is testing a genuinely different primitive.

Agent margins Tri has to prove:

| Agent need | Existing answer | Tri proof |
| --- | --- | --- |
| Mutate before a commit exists | Entire explains the session after it is attached to Git; GitButler records edits into branches or commits | File writes, command runs, snapshots, and checks are first-class Tri events before export |
| Run multiple attempts from one base | Git worktrees or GitButler branches keep attempts separated at the Git level | Durable workspaces isolate attempts without creating branches, while tasks compare their changesets directly |
| Preserve machine-readable review evidence | Entire gives session context; GitButler gives branch/PR organization | Changesets carry typed operations, checks, artifacts, policy decisions, review actions, and export previews |
| Protect private or embargoed work | Entire relies on checkpoint branches plus redaction; GitButler follows repo/branch/PR visibility | Object-level policy controls reads and exports for paths, changesets, reviews, logs, artifacts, releases, and advisories |
| Use secrets during checks | Agents and hooks can capture context that later needs redaction | Secret values stay outside snapshots; leases and generated config artifacts are auditable and non-exportable |
| Publish to GitHub | GitHub/GitLab PRs remain the collaboration center | GitHub PRs are generated export artifacts after review and policy pass |

The product claim should stay modest:

```text
Forges (Entire, Origin, GitHub) make Git faster and attach agent review to it.
GitButler makes Git branches tolerable.
Scion stops shipping a full clone to every agent.
Tri makes the workspace, the changeset, and the policy decision the source of truth.
```

Tri should be judged against them with concrete tests:

- Can Tri run three agents on the same base with isolated files and checks, without Git worktrees or one shared working directory?
- Can Tri preserve agent context before a commit exists?
- Can Tri create a reviewable changeset without creating a branch?
- Can Tri hide one changeset in a public project without moving metadata to a separate private repo?
- Can Tri run a check with scoped config without ever writing secret-bearing files into source history?
- Can Tri export to Git only after policy and review pass?

## Agent-Scale Gaps

Being the best agent-native source control means closing the gaps that Scion and the forges expose. Each gap is listed with its state in this repository.

### Closed In The Prototype

| Gap | Why it matters at agent scale | What Tri does now |
| --- | --- | --- |
| Same-file merges | Two agents editing different lines of one file conflicted at the path level, so conflicts grew with merge volume | `merge_entries` runs a line-level three-way merge for text files (`tri/textmerge.py`). Disjoint hunks merge; overlapping or adjacent hunks, binaries, symlinks, and mode clashes still conflict. Merges and workspace updates report `text_merged_paths`. |
| Human review on every merge | At 30,000 merges a day, a required human approval is the bottleneck | Maintainers declare merge rules (`tri merge-rules add <project> <name> --path <glob> [--exclude <glob>] [--allow <selector>] [--max-files <n>]`). A changeset whose every changed path matches a rule merges without a review. Checks, conflicts, secrets, hidden paths, stacks, and embargoes still gate it, and advisory-linked changesets never qualify. The decision records `merge_rule:<id>` as its grant. Exports still need an approved review. |
| Merge races | Concurrent merges race on one target compare-and-swap; the loser failed back to the agent | `tri changeset merge` recomputes against the moved target and retries (`--retry`, default 5) before reporting `stale_target_snapshot`. There is no global merge lock. |
| History needs a checkout | Agents and CI asked Git for blame or merge bases by cloning | `tri files log <ref> <path>`, `tri files blame <ref> <path>`, and `tri snapshots merge-base <a> <b>` run on the store. Blame attributes lines to the snapshot, actor, and changeset that introduced them. History walks stop at the first snapshot the actor cannot read, and hidden or secret-like paths are refused. |
| Self-asserted actors | Any HTTP client could claim `actor_local`, which holds maintainer and security roles, so the API could not safely leave one machine | `tri auth issue <actor> --file tokens.json` issues a bearer token bound to one actor and stores only its SHA-256. `tri serve --auth-file tokens.json` pins every request to the token's actor and rejects a different `--actor`, payload actor, batch actor, or `X-Tri-Actor` with `actor_mismatch`. Revocation applies on the next request. `tri serve` refuses to bind beyond loopback without `--auth-file` unless `--allow-unauthenticated` is passed. `serve`, `mcp`, and `auth` cannot run through the API or a batch. |
| No MCP server | Claude Code, Codex, and Cursor reach tools through MCP; GitButler ships `but mcp` | `tri --actor agent:<id> mcp` serves the `tri` and `tri_batch` tools over stdio. Every call is pinned to the startup actor. |
| Whole-tree runs on large repositories | On a 50,000-file tree a warm `tri run` spent 7.7 s deleting its directory, and every merge to the target forced a 72 s rebuild of the cached base before the next run | `tri run --paths <glob>` and `tri workspace mount --paths <glob>` lay out only matching files (a partial checkout); writes anywhere still import, and only laid-out files can be deleted, through sync, salvage, and the reaper. Released run directories are renamed into the store's trash and deleted by a detached process (`TRI_DEFER_CLEANUP=0` deletes inline; the reaper and `tri gc --reap` empty leftovers). A new base directory is cloned from the most recently used base and patched with the difference. On that tree: first edit about 1 s (unchanged; a workspace is a row), warm full run 11.5 s to 3.7 s, first run after a merge about 72 s to 3.1 s, partial run of one 250-file package about 1.9 s. |
| Unsandboxed runs | `tri run` executed workspace code as the server user, so any token holder had remote code execution with the server's files, credentials, and network | `tri run --sandbox`, or `TRI_RUN_SANDBOX=auto|required` (`tri serve --run-sandbox`, `tri mcp --run-sandbox`), runs commands under bubblewrap on Linux or the macOS sandbox. The command sees the host read-only except its run directory, cannot read the Tri home (blobs of hidden files, secret values) or credential files under the user's home (`.ssh`, `.aws`, `.config/gh`, ...), has no network (`TRI_RUN_SANDBOX_NETWORK=1` allows it; env installs default to network on), starts from a scrubbed environment with HOME and TMPDIR inside the run directory, and runs under `--timeout` / `TRI_RUN_TIMEOUT`, `TRI_RUN_CPU_SECONDS`, `TRI_RUN_MEMORY_MB` (Linux), and `TRI_RUN_FILE_SIZE_MB`; a timeout kills the whole process group. Runs, private env installs, env builds, and changeset revalidations all use it. `tri serve` beyond loopback defaults to `required` and refuses to start on a host without isolation unless `--allow-unsandboxed-runs` is passed. Check runs record the sandbox in `environment_policy.sandbox`, and `tri doctor` reports the backend. |
| Every merge waits for checks | Scion verifies after merge and before deploy; pre-merge checks put a test run in front of every one of thousands of daily merges | `tri merge-rules add ... --verify-after <check-set>` lets a changeset that matches the rule merge before its checks run, as long as none has failed. The merge queues a verification. `tri verify run <project> [--watch]` (maintainers or `role:service`) runs the check set on each merged snapshot in merge order, with earlier reverted changesets removed so a later merge is not blamed for them. A failure reverts that changeset on the current target with a three-way merge, so later merges are kept; a revert that conflicts is reported as `revert_conflict` and leaves the target alone. Reverted changesets reopen their workspace, the mirror titles the revert commit, and exports still require passing checks. |
| Every machine held a full store | Agents on many machines each needed their own copy of metadata and objects, so nothing was authoritative and disks were not caches | Remote mode: with `TRI_REMOTE` and a token (`TRI_TOKEN` or `TRI_TOKEN_FILE`), the CLI and `tri mcp` send every command to one `tri serve`; the client opens no store. `tri workspace mount` lists the workspace on the server, fetches only content missing from the per-machine blob cache (`TRI_CACHE_DIR`), verifies each hash, and writes the files (`--paths` for a partial mount); `tri workspace sync` uploads what changed in one batch guarded by the mounted head, and reports `stale_materialization` if the workspace moved. `files write --body` uploads client files. Runs execute on the server. Over an authenticated or non-loopback API, arguments naming server paths are refused for non-maintainers (`policy_denied`, `reason: server_path`); before this, `files write --body <server file>` or `import <server dir>` let any token holder read the server's disk. |
| Runs executed next to the store | Even sandboxed, a kernel or sandbox escape on the service host reaches every project's objects and secrets | `tri run --runner remote` (or `tri serve --run-executor remote` for every run) queues a run job instead of executing it; `--wait <s>` (default `TRI_RUN_WAIT`, 600) returns the check when it finishes. Runner machines with a `role:service` token run `tri runner work` against `TRI_REMOTE`: they claim the oldest job (a lease re-queues jobs from runners that die), fetch only the job's files through job-scoped `tri runner fetch`, from their own blob cache when possible, run the command in their own sandbox with a scrubbed environment (the runner token never reaches the job, and the cache and token file are hidden), and report output and file changes with `tri runner complete`. The server records the check run (`runner: remote:<runner>`), applies the changes only if the workspace head has not moved (`stale_workspace_head` otherwise), and never lets a runner write or delete paths hidden from the requester. Runners only see paths the requester can read, and agents cannot claim jobs. |
| No regional cache | Many machines in one region fetched the same blobs across the WAN | `tri cache serve --upstream <url>` forwards every request with the caller's own token, so the service still authenticates and authorizes everything. For `files fetch` and `runner fetch` it asks the service for authorized hashes only (`--hashes-only`) and requests content just for hashes its shared content-addressed store lacks, so content crosses the WAN once per region and a client only receives bytes the service has just authorized. Clients point `TRI_REMOTE` at the cache. |
| No two-way Git mirror | Teams adopt only when teammates who never install Tri still see an ordinary Git repository | `tri mirror add <project> --remote <url>` pairs a target with a Git branch; `tri mirror sync [--watch]` turns new Git commits into attributed snapshots, three-way merges them with Tri work (line-level), and pushes each Tri merge back as one Git commit titled by its changeset, plus a merge commit when both sides moved. Pushes are never forced; a Git push that races the sync is refetched and retried. Paths the mirror audience cannot read (private packages) and secret-like paths never leave Tri, and Git cannot overwrite them. Same-line edits on both sides stop with `mirror_conflict` and change nothing. |

### Still Open

These are ordered by how much they block the goal.

1. **Replicated storage behind the service.** Clients now treat one `tri serve` as the authority, but that service is a single process on one machine with SQLite and local blob files: no replication, failover, or horizontal scale. `tri backup create --watch` keeps a consistent warm standby that is itself a Tri home, which covers disaster recovery but not failover. The target is Scion's shape: a transactional metadata store (Postgres or Spanner class) and object storage behind stateless API nodes.
2. **Virtual checkouts.** Partial checkouts (`--paths`), derived bases, remote partial mounts, a per-machine blob cache, and a regional cache shipped, but the caller still names the paths up front. Agents should fault in trees and blobs on first access through a FUSE (Linux, macFUSE) or ProjFS (Windows) view; that needs a native filesystem driver, which this stdlib-only prototype does not ship.
3. **Runner hardening.** Remote runners execute in bubblewrap or the macOS sandbox; microVM isolation (Firecracker), secret leases for remote jobs (refused today), autoscaling, and a Windows isolation backend are not built.
4. **Production runtime.** The prototype is one large Python module. It is the right tool for proving the object model; the server should be rewritten once the object model stops changing, keeping the JSON contracts as the compatibility boundary.

## Agent-Scale Scorecard

Tri is the best agent-native source control only when it wins these measurements against Git worktrees plus GitHub, GitButler, and Entire, and stays within reach of Scion's published numbers. Every row now has a measurement: `tri-bench` covers the workflow loop, `tri-bench-fanout` parallel attempts, `tri-bench-contention` the merge rows, and `tests/test_leak_sweep.py` the leak row. Scion publishes scale (30,000 merges a day on one monorepo) rather than per-merge numbers, so the comparison with it is shape, not a race.

| Measurement | Bar | Status |
| --- | --- | --- |
| Parallel attempts from one base | 100 or more agents with isolated files and checks, no shared working directory | fan-out benchmark at 100 attempts, 8 at a time (2,000 source files, 5,000 dependency files): 100 of 100 checks passed; Tri ended at 63 MiB of disk versus 3.0 GiB for Git worktrees, with the same wall time (150 s versus 158 s); `benchmark-results/fanout-100/` |
| Time to first edit on a large repository | seconds, independent of history size | about 1 s on a 50,000-file tree (workspace create plus first write); partial runs about 1.9 s; first full run after a merge 3.1 s on APFS |
| Merges per hour under contention | no global lock; same-file disjoint edits merge without a person | contention benchmark, 50 agents 8 at a time on 20 shared files: 24,805 merges/hour, 48 of 50 merged with no review and no lost lines (p95 merge 0.20 s), versus 4,386 for Git pull-rebase-push (19 agents ran out of retries) |
| Conflict rate per 1,000 merges | lower than path-level merging on the same workload | 40 per 1,000 with line merges versus 420 with path-level merging on the same workload; every line-merge conflict was a deliberate same-line overlap |
| Leak tests | no hidden path, secret, private changeset, or embargoed advisory escapes through any read, search, diff, history, log, artifact, export, or API route | `tests/test_leak_sweep.py`: one fixture with a private package path, a secret-like path, a leased secret, another actor's private workspace, a private changeset in a public workspace, and an embargoed advisory, each carrying canaries in names, contents, titles, and values; an unprivileged agent runs every read-shaped command with each output flag through the CLI, `tri batch`, the HTTP GET routes, `POST /v0/command`, and `POST /v0/batch`, follows each denial into `policy explain`, and no canary or private id may come back; a guard derived from `COMMAND_CONTRACTS` fails when a new read command is neither swept nor exempted |
| History without checkout | log, blame, and merge base answered by the store | shipped |
| Agent harness reach | MCP and HTTP with authenticated, pinned actors | shipped |

## Response To Theo's Rant

### Partial Open Source

Open source should not mean every file, draft, security fix, and in-flight change is public at the same time.

Tri supports visibility on:

- projects
- package boundaries
- paths
- files
- snapshots
- workspaces
- changesets
- reviews
- tasks
- check runs
- release artifacts

This allows a public project to contain private packages, private draft work, embargoed security fixes, and maintainer-only reviews without splitting into many repos.

### Secrets And `.env`

Secrets are not source files.

Tri treats secrets and config as policy-bound objects. A workspace can request a generated config view or a secret lease for a check run, but raw secret values are never part of the source snapshot and are never exported to Git.

### Hidden In-Flight Work

In-flight work is a workspace or changeset with a visibility policy. It does not have to become a public branch or public PR before it is ready.

### Embargoed Security Fixes

Security fixes are private changesets attached to advisories. They can run private checks, collect trusted review, prepare backports, and publish only when the release is ready.

### Commits And Branches

Commits are useful interchange artifacts. They are bad as the live authoring primitive for agents.

Tri uses:

- automatic snapshots for state
- workspaces for parallel work
- operations for intent and provenance
- changesets for review
- Git commits only at export boundaries

Branches become optional target labels, not exclusive checked-out state.

### Worktrees

Worktrees are a filesystem workaround for missing workspace semantics.

Tri workspaces are cheap copy-on-write views over snapshots. A materialized directory can exist, but it is a cache. The workspace identity, state, history, checks, and review context live in Tri.

### API-First Source Control

Agents should read, write, diff, search, check, review, and export code through structured APIs. Shell and filesystem projections remain useful for humans and legacy tools, but they are not the primary interface.

## Architecture Layers

Tri has seven layers.

### 1. Content Store

Stores immutable content-addressed objects:

- blobs
- trees
- snapshots
- artifacts

The content store answers: "What bytes and tree structure existed?"

### 2. Metadata Store

Stores named objects and indexes:

- projects
- targets
- workspaces
- tasks
- changesets
- reviews
- policies
- actors
- check runs
- secret references
- advisories
- releases

The metadata store answers: "What does this content mean, who can see it, and why does it exist?"

### 3. Event Log

Stores append-only events:

- workspace created
- file written
- file deleted
- command run
- snapshot captured
- changeset created
- policy evaluated
- review requested
- secret lease granted
- check completed
- export produced

The event log answers: "What happened, in what order, by whom, with what authority?"

### 4. Workspace Engine

Maintains mutable copy-on-write overlays on top of immutable snapshots.

The workspace engine answers: "What does this actor currently see and edit?"

### 5. Change Graph

Derives changesets, stacks, merge plans, conflict objects, and export patches from snapshot differences and workspace operations.

The change graph answers: "What unit of work is ready to review or merge?"

### 6. Policy Engine

Enforces object-level visibility, secret access, export rules, check requirements, review requirements, and advisory embargoes.

The policy engine answers: "Is this actor allowed to see, change, run, publish, or export this object?"

### 7. Collaboration Layer

Builds GitHub-like workflows on top of Tri primitives:

- tasks
- reviews
- checks
- releases
- packages
- advisories
- search
- provenance

The collaboration layer answers: "How do humans and agents coordinate work safely?"

## Core Invariants

These invariants matter more than the exact first implementation.

### Snapshots Are Immutable

A snapshot is never edited. Any change creates a new snapshot.

### Workspaces Are Mutable Views

A workspace is the mutable thing agents use while working. It has a base snapshot, an overlay, a current head snapshot, an owner, a task, and an event stream.

### Every Mutation Is Recorded

Every write, delete, move, permission change, generated file insertion, command boundary, and export is recorded as an event with actor identity and policy context.

### Snapshots Are Automatic

Agents do not decide when history exists. Tri snapshots after mutations and command boundaries. Human-authored checkpoint names are optional metadata.

### Changesets Are Review Units

A changeset is derived from a base snapshot, a head snapshot, and selected operations. It is not the same thing as a commit and does not need to map one-to-one to a Git commit.

### Secrets Never Enter Source History

Secret values, raw environment files, private service payloads, and privileged credentials are never stored as source blobs. Tri stores references, leases, fingerprints, scopes, and redacted audit events.

### Policy Applies To Reads And Exports

Visibility is not just a write-time concern. Reads, diffs, searches, reviews, check logs, artifacts, and Git exports all pass through policy.

### Disk Is A Cache

A materialized filesystem workspace can be thrown away and recreated from the Tri store. It is not the authoritative state.

### Git Is An Adapter

Tri can import Git commits and export Git patches or commits. It should not inherit Git's branch/worktree/commit mental model internally.

## System Contracts

The architecture needs hard contracts early. These are the rules an implementation should preserve even if the first storage engine is simple.

### IDs And References

Object ids should be stable, typed, and cheap for agents to inspect.

Recommended id prefixes:

- `proj_` for projects
- `blob_` for blobs
- `tree_` for trees
- `snap_` for snapshots
- `target_` for targets
- `ws_` for workspaces
- `op_` for operations
- `evt_` for events
- `task_` for tasks
- `run_` for task runs
- `cs_` for changesets
- `review_` for reviews
- `check_` for check runs
- `artifact_` for logs, patches, generated files, and check artifacts
- `secret_` for secret references
- `lease_` for materialization and secret leases
- `policy_` for policies
- `decision_` for policy decisions
- `actor_` for actors
- `adv_` for advisories
- `rel_` for releases
- `export_` for Git exports

Content-addressed objects also carry a content hash. Metadata ids should not be content hashes because metadata can evolve while content remains immutable.

### Path Normalization

All tree paths must be normalized before hashing, policy evaluation, diffing, or export.

Rules:

- paths are UTF-8 strings
- `/` is the separator
- no absolute paths
- no empty segments
- no `.` or `..` segments
- no platform-specific separators in stored paths
- no case folding in storage
- symlink targets are stored as blob metadata, not followed during tree hashing

Materializers translate normalized Tri paths into host filesystem paths and must reject paths that escape the mount root.

### Snapshot Transaction

Every mutation creates a new snapshot in one transaction.

Transaction steps:

1. load workspace state and expected head snapshot
2. evaluate read/write policy
3. write new blobs
4. write new tree objects
5. write new snapshot
6. write operation record
7. append event records
8. update workspace head with compare-and-swap on the expected head

If the compare-and-swap fails, the mutation must fail with a stale-workspace diagnostic. The caller can retry after inspecting the new head.

### Command Transaction

Commands are tracked as check runs even when they are not formal CI.

Command execution creates at least two events:

- command started (`check.started`, `check.rerun_started`, or `check.revalidation_started`)
- command finished (`check.completed`, `check.rerun_completed`, or `check.revalidated`)

The check run records:

- snapshot before
- materialized directory fingerprint, if mounted
- command and arguments
- environment policy
- secret leases
- stdout/stderr log refs
- artifact refs
- exit status
- snapshot after
- file changes observed

If a command mutates files in a materialized workspace, Tri must import those changes back through the same mutation pipeline. Filesystem side effects should not silently bypass operations and events.

### Concurrency

Tri should use optimistic concurrency for workspace heads.

Rules:

- two agents may read the same snapshot concurrently
- two agents should not mutate the same workspace head without compare-and-swap
- separate workspaces never block each other
- target updates are compare-and-swap operations on target pointers
- changeset creation pins base and head snapshots
- export pins the reviewed changeset snapshot

Conflicts should appear when reconciling snapshots or updating workspace bases, not as corrupted workspace state.

### Materialization Contract

A materialized workspace is a cache with a lease.

The lease records:

- workspace id
- mounted path
- head snapshot at mount time
- last sync snapshot
- ignored paths
- generated config artifacts
- lease owner
- lease expiry

On sync, Tri compares the materialized directory against the last sync snapshot, imports changes as operations, and updates the lease. If the workspace head moved independently, sync requires a merge or fails with a stale-materialization diagnostic.

Agents should prefer explicit lease ids when syncing:

```sh
tri workspace mount <workspace> ./mounted --ttl-seconds 3600
tri workspace leases <workspace>
tri workspace sync <workspace> --lease <lease>
tri workspace lease-revoke <lease>
```

If `tri workspace sync` is called without `--lease`, Tri may use the caller's own active materialization lease. Multiple active leases from different actors are ambiguous and must fail closed until the caller supplies the intended lease id.

### Diff Contract

A diff should be reproducible from snapshots.

Diff inputs:

- base snapshot
- head snapshot
- path filter
- visibility policy
- diff mode: summary, patch, file list, machine

Diff outputs:

- added paths
- modified paths
- deleted paths
- renamed paths, if detectable
- binary paths
- redacted paths
- policy-denied paths
- patch hunks where allowed
- content fingerprints

Visible file entries include `old_content_fingerprint`, `new_content_fingerprint`, and `binary` so agents can compare file identity and avoid treating binary changes as text.

Agents should be able to request a small summary first and expand specific files later.

The CLI and HTTP API support `--mode summary|files|patch|machine` and repeatable `--path <glob>` filters for that flow.

### Policy Decision Contract

Every policy decision should be explainable.

Decision fields:

- allowed
- actor
- action
- object
- policy ids evaluated
- grants used
- denials hit
- redactions applied
- reason code
- event id

Policy failures should be structured diagnostics, not prose.

### Output Contract

Every CLI command should support JSON output from the beginning.

Common fields:

- `ok`
- `schema`
- `schema_version`
- `command`
- `actor`
- `events`
- `diagnostics`
- `warnings`
- `cost`
- `refs`
- `links`

Human-readable terminal output can be derived from the structured response. Agents should not depend on parsing pretty output.

## First JSON Schemas

The first implementation should ship versioned schemas before it has a polished UI. These are not final hosted API schemas; they are the minimum command contracts agents can depend on.

### Response Envelope

Every JSON command returns the same envelope.

```json
{
  "ok": true,
  "schema": "tri.workspace.inspect",
  "schema_version": "0.1.0",
  "command": ["tri", "workspace", "inspect", "ws_123"],
  "actor": "actor_local",
  "data": {},
  "events": ["evt_123"],
  "diagnostics": [],
  "warnings": [],
  "cost": {
    "wall_ms": 12,
    "input_bytes": 0,
    "output_bytes": 1200,
    "external_cost": "0 USD"
  },
  "refs": {},
  "links": {}
}
```

Rules:

- `ok=false` still uses the same envelope.
- `data` is omitted only when no partial data exists.
- `diagnostics` are structured and stable enough for agents to branch on.
- `events` contains event ids created by the command.
- `refs` contains object references that let agents fetch more detail without parsing prose.
- `refs` is populated only from ids already visible in the response envelope, diagnostics, or event list; it must not add hidden object ids that policy redacted from `data`.
- `links` is derived from `refs` and gives each visible object id a generic inspect command and HTTP route such as `tri objects inspect <object>` and `/v0/objects/<object>`.
- `links` must not introduce ids that are absent from `refs`; if an object id is policy-redacted from `refs`, it is also absent from `links`.

### Diagnostic

```json
{
  "code": "stale_workspace_head",
  "severity": "error",
  "message": "Workspace head changed before mutation could commit.",
  "object": "ws_123",
  "expected": "snap_old",
  "actual": "snap_new",
  "retryable": true
}
```

Initial diagnostic codes:

- `stale_workspace_head`
- `stale_materialization`
- `policy_denied`
- `path_not_found`
- `path_escaped_mount`
- `invalid_path`
- `secret_export_blocked`
- `non_exportable_artifact`
- `conflict_required`
- `unknown_actor`
- `missing_required_check`
- `missing_review_approval`
- `git_export_failed`

### Snapshot Ref

```json
{
  "id": "snap_123",
  "project": "proj_123",
  "root_tree": "tree_123",
  "parents": ["snap_base"],
  "source": "workspace",
  "content_fingerprint": "sha256:...",
  "created_by": "actor_agent_a",
  "created_at": "2026-05-28T00:00:00Z"
}
```

### Workspace Inspect

```json
{
  "id": "ws_123",
  "project": "proj_123",
  "base_snapshot": "snap_base",
  "head_snapshot": "snap_head",
  "owner": "actor_agent_a",
  "task": "task_123",
  "task_run": "run_123",
  "status": "active",
  "visibility": ["private", "task-only"],
  "hidden_paths": ["private/**"],
  "last_operation": "op_456",
  "materializations": [
    {
      "path": "./tmp/fix-tests",
      "lease": "lease_123",
      "last_sync_snapshot": "snap_head"
    }
  ],
  "checks": ["check_123"],
  "changesets": ["cs_123"]
}
```

### Task Inspect

```json
{
  "id": "task_123",
  "project": "proj_123",
  "title": "Fix checkout total rounding",
  "body": "Make checkout totals deterministic.",
  "requester": "human_alice",
  "status": "running",
  "acceptance_criteria": ["regression test passes"],
  "required_checks": [
    {
      "name": "npm test -- checkout",
      "command": ["npm", "test", "--", "checkout"]
    }
  ],
  "workspaces": ["ws_123"],
  "runs": ["run_123"],
  "changesets": ["cs_123"]
}
```

### File Diff

```json
{
  "base_snapshot": "snap_base",
  "head_snapshot": "snap_head",
  "summary": {
    "added": 1,
    "modified": 2,
    "deleted": 0,
    "renamed": 0,
    "redacted": 1,
    "policy_denied": 1
  },
  "files": [
    {
      "path": "src/checkout.ts",
      "status": "modified",
      "old_blob": "blob_old",
      "new_blob": "blob_new",
      "patch_ref": "artifact_patch_123",
      "visible": true
    },
    {
      "path": null,
      "path_hash": "sha256:...",
      "status": "modified",
      "old_blob": null,
      "new_blob": null,
      "visible": false,
      "redaction": "hidden_path"
    }
  ]
}
```

### Check Run Inspect

```json
{
  "id": "check_123",
  "workspace": "ws_123",
  "changeset": "cs_123",
  "task_run": "run_123",
  "status": "passed",
  "runner": "local",
  "command": ["npm", "test"],
  "snapshot_before": "snap_before",
  "snapshot_after": "snap_after",
  "exit_status": 0,
  "duration_ms": 4210,
  "logs": {
    "stdout": "artifact_stdout",
    "stderr": "artifact_stderr"
  },
  "artifacts": ["artifact_junit"],
  "secret_leases": ["lease_test_env"],
  "file_changes": ["op_789"],
  "reproducibility": "local_replayable"
}
```

### Changeset Inspect

```json
{
  "id": "cs_123",
  "project": "proj_123",
  "title": "Fix checkout total rounding",
  "status": "ready",
  "base_snapshot": "snap_base",
  "head_snapshot": "snap_head",
  "operations": ["op_1", "op_2"],
  "task_run": "run_123",
  "visibility": ["private"],
  "hidden_paths": ["private/**"],
  "touched_paths": ["src/checkout.ts", "test/checkout.test.ts"],
  "redacted_touched_paths": 1,
  "checks": [{"id": "check_123", "status": "passed", "artifacts": ["artifact_stdout"]}],
  "reviews": ["review_123"],
  "policy_decisions": ["decision_123"],
  "export_status": "not_exported",
  "suggested_split_points": [
    {
      "kind": "path",
      "paths": ["test/**"],
      "reason": "test-only changes can review separately"
    }
  ]
}
```

### Review Inspect

```json
{
  "id": "review_123",
  "changeset": "cs_123",
  "status": "open",
  "reviewers": ["human_reviewer"],
  "check_summary": {"total": 1, "passed": 1, "failed": 0},
  "changeset_summary": {
    "id": "cs_123",
    "title": "Fix checkout total rounding",
    "status": "reviewing",
    "touched_paths": ["src/checkout.ts"],
    "redacted_touched_paths": 0,
    "suggested_split_points": []
  },
  "task": {
    "id": "task_123",
    "title": "Fix checkout total rounding",
    "acceptance_criteria": ["regression test passes"],
    "required_checks": [{"command": ["npm", "test", "--", "checkout"]}]
  },
  "commands": [{"check": "check_123", "command": ["npm", "test"], "status": "passed"}],
  "checks": [{"id": "check_123", "status": "passed", "artifacts": ["artifact_stdout"]}],
  "artifacts": [{"check": "check_123", "artifact": {"id": "artifact_stdout", "kind": "stdout"}}],
  "policy_findings": {"decision": {"allowed": false}, "diagnostics": []},
  "visibility_impact": {"hidden_paths": [], "redacted_touched_paths": 0},
  "actions": []
}
```

### Policy Decision

```json
{
  "id": "decision_123",
  "allowed": false,
  "actor": "actor_agent_a",
  "action": "export.git_patch",
  "object": "cs_123",
  "policies": ["policy_no_secret_export"],
  "grants": [],
  "denials": ["non_exportable_artifact"],
  "redactions": [],
  "reason_code": "secret_export_blocked",
  "event": "evt_999"
}
```

## Object Model

### Project

A project is a codebase with identity, package boundaries, policies, targets, import settings, and export settings.

Project fields:

- id
- name
- default target
- package boundaries
- visibility defaults
- policy set
- Git import/export settings

The local MVP stores package boundaries as named path-glob groups with visibility and owner metadata. Agents can inspect readable boundaries with `tri packages list`, `tri packages inspect`, or the generic object route. Private package boundaries automatically hide matching files from non-owners across file reads, diffs, search, changeset views, operation details, event views, reviews, and export policy; `--hide-package` remains available for explicit workspace or changeset hiding.

### Blob

A blob is immutable file content.

Blob fields:

- content hash
- byte length
- media type guess
- encryption state
- storage location

### Tree

A tree maps names to blobs, subtrees, or tombstones.

Tree fields:

- content hash
- entries
- file mode metadata
- normalized paths

### Snapshot

A snapshot is an immutable tree plus provenance.

Snapshot fields:

- id
- project
- root tree
- parent snapshots
- source kind: imported, workspace, merge, export, release
- created by
- created at
- event range
- content fingerprint

Raw snapshot ids are not a policy bypass. Imported and target snapshots are readable project source, while workspace, operation, check, and changeset snapshots inherit access and hidden-path policy from the owning workspace or changeset.

### Target

A target is a movable label that points at a snapshot.

Examples:

- `main`
- `release/2026-05`
- `prod`
- `staging`

Targets replace the useful part of branches without making checked-out branch state exclusive.

### Workspace

A workspace is a mutable copy-on-write view over a base snapshot.

Workspace fields:

- id
- project
- base snapshot
- head snapshot
- overlay
- owner actor
- task
- visibility policy
- status: active, paused, archived, discarded, promoted
- ttl
- materialization cache path, if any

### Operation

An operation is an actor-intended mutation in a workspace.

Operation kinds:

- write file
- delete file
- move file
- copy file
- chmod
- generate file
- apply patch
- resolve conflict
- update base
- run command

Operation fields:

- id
- workspace
- actor
- parent operation
- input snapshot
- output snapshot
- path set
- policy grants used
- timestamp

### Event

An event is the append-only audit record for something that happened.

Events are lower-level than operations. One operation can emit multiple events.

Event listing, inspection, and secret audit views must be policy filtered by the event's owning workspace, changeset, task, release, advisory, or artifact. Actors can inspect their own events, maintainers can inspect all events, and hidden or secret-like paths in event refs are redacted rather than leaked.

Listing and audit views count redacted events passively. Direct event inspection records an explainable deny decision when access is refused.

Event fields:

- id
- actor
- project
- workspace
- task
- object refs
- event kind
- input fingerprints
- output fingerprints
- policy result
- timestamp

### Task

A task is an executable work request.

Task fields:

- id
- title
- prompt or issue body
- requester
- assigned actors
- spawned workspaces
- acceptance criteria
- required checks
- related reviews
- status

A task can spawn multiple competing workspaces. Tri should make comparison between attempts first-class.

Task specs can be public, but attempt references are not automatically public. `tri task inspect` and `tri task attempts` must filter workspace, task-run, and changeset references the actor cannot read, counting omitted private attempts without leaking their ids.

### Task Run

A task run is one actor's attempt to satisfy a task.

Task run fields:

- id
- task
- actor
- model or tool identity
- workspace
- status
- commands
- costs
- logs
- artifacts
- assumptions
- final recommendation
- resulting changesets

### Changeset

A changeset is a reviewable unit of change.

Changeset fields:

- id
- project
- base snapshot
- head snapshot
- status
- selected operations
- touched paths
- visibility policy
- task run
- check runs
- review status
- export status
- conflicts
- suggested split points

Changesets can be:

- split
- stacked
- merged
- hidden
- published
- exported
- superseded
- discarded

### Review

A review is a decision surface for a changeset.

Review fields:

- id
- changeset
- status
- reviewers
- comments
- requested follow-up runs
- policy findings
- check summary
- risk summary
- approvals
- required changes
- export preview

Review centers on the changeset, not a branch.

### Check Run

A check run is structured execution evidence.

Check run fields:

- id
- task run
- workspace or changeset
- status
- command
- runner
- environment
- inputs
- secret leases
- outputs
- logs
- artifacts
- exit status
- duration
- reproducibility status
- actor
- cost

Agents should query check objects, not scrape CI logs.

### Secret

A secret is a policy-bound capability, not a file.

Secret fields:

- id
- provider
- environment
- scope
- allowed actors
- allowed tasks
- allowed checks
- lease ttl
- audit events

Tri may generate config files for a workspace, but generated config files are marked non-exportable unless policy explicitly allows them.

### Policy

A policy controls visibility, authority, checks, exports, and release behavior.

Policy scopes:

- project
- package
- path
- file
- workspace
- changeset
- review
- task
- check
- secret
- advisory
- release

Policy decisions should be recorded as events so agents can explain why access or export was allowed or denied.

### Actor

An actor is a human, agent, service, or automation.

`task:<task-id>` is a constrained execution identity for automation assigned to one task. It can run that task and mutate workspaces or changesets attached to that task, but it cannot use public task execution as a way to start or inspect unrelated work.

Task-scoped actors also cannot import projects or create new top-level tasks. A task actor is an execution capability, not a project-level identity.

The local MVP exposes derived actor identity through `tri actors inspect <actor>`. This is not a hosted identity system; it lets agents inspect the local roles, selectors, principal, and task scope that Policy v0 will use before they attempt source-control operations.

Actor fields:

- id
- kind
- provider
- model or version
- owner
- permissions
- trust grants
- cost model
- signing key or identity binding

Every mutation, review, check, and export is attributable to an actor.

### Advisory

An advisory is private security coordination attached to changesets.

Creating advisory-linked work requires advisory read access. A random agent can report its own advisory, but cannot attach workspaces or changesets to someone else's embargoed advisory by guessing its id or title.

Advisory fields:

- id
- status
- affected packages
- embargo policy
- private changesets
- trusted reviewers
- private checks
- backports
- coordinated release plan
- disclosure state

### Release

A release is an approved set of changesets, artifacts, packages, checks, and advisories.

Release fields:

- id
- status
- target snapshot
- included changesets
- packages
- artifacts
- checks
- advisories disclosed
- rollback target
- Git export refs

### Export

An export is a policy-gated projection of a changeset into an external source-control format.

Export fields:

- id
- status
- changeset
- export kind: git_patch, git_commit, git_branch, github_pr
- target remote, if any
- target ref, if any
- exported object ids
- policy decision
- actor
- created at
- completed at
- diagnostics
- source event range
- re-export determinism metadata: stable input fingerprint, content fingerprint when available, repeat export link, same-output comparison, and reason code when external Git or forge state prevents deterministic replay

## Lifecycle State Machines

State transitions should be explicit from the first implementation. Agents need to know which actions are legal without reverse-engineering UI conventions.

### Workspace States

Workspace states:

- `active`: accepts mutations and check runs
- `paused`: readable but rejects mutations until resumed
- `archived`: immutable historical workspace
- `discarded`: abandoned workspace; readable by authorized actors for audit
- `promoted`: workspace has produced an accepted changeset or target update

Allowed transitions:

- `active -> paused`
- `paused -> active`
- `active -> archived`
- `paused -> archived`
- `active -> discarded`
- `paused -> discarded`
- `active -> promoted`
- `promoted -> archived`

Rules:

- only `active` workspaces may mutate, run checks, or create changesets
- discarded workspaces cannot export
- promoted workspaces cannot mutate unless forked into a new workspace
- archived workspaces can be materialized read-only

### Task States

Task states:

- `open`
- `running`
- `blocked`
- `reviewing`
- `accepted`
- `rejected`
- `closed`

Allowed transitions:

- `open -> running`
- `running -> reviewing`
- `running -> blocked`
- `blocked -> running`
- `reviewing -> accepted`
- `reviewing -> rejected`
- `accepted -> closed`
- `rejected -> running`
- `rejected -> closed`

Rules:

- a task can have many task runs
- accepted tasks must identify accepted changesets or a no-change resolution
- blocked tasks must record the blocking diagnostic or missing input

### Task Run States

Task run states:

- `started`
- `mutating`
- `checking`
- `summarizing`
- `completed`
- `failed`
- `cancelled`

Rules:

- a task run owns one primary workspace
- a task run can produce zero or more changesets
- failed and cancelled runs keep their snapshots, checks, and events
- completed runs must record a final recommendation

### Changeset States

Changeset states:

- `draft`
- `ready`
- `reviewing`
- `changes_requested`
- `approved`
- `merged`
- `exported`
- `superseded`
- `discarded`

Allowed transitions:

- `draft -> ready`
- `ready -> reviewing`
- `reviewing -> changes_requested`
- `changes_requested -> ready`
- `reviewing -> approved`
- `approved -> merged`
- `approved -> exported`
- `ready -> superseded`
- `reviewing -> superseded`
- `draft -> discarded`
- `ready -> discarded`

Rules:

- only `ready`, `reviewing`, and `approved` changesets can export previews
- only `draft` or `ready` changesets can accept a new review
- export requires approved policy, unless an explicit local-only override is recorded
- superseded changesets must point to the replacement changeset
- discarded changesets remain readable to authorized actors for audit

### Review States

Review states:

- `open`
- `waiting_for_checks`
- `waiting_for_reviewers`
- `changes_requested`
- `approved`
- `closed`

Rules:

- review state derives partly from required checks and reviewer decisions
- a review cannot become `approved` while required checks are failing or missing
- requesting another agent pass creates a new task run linked to the review
- closing a review does not delete the changeset

### Check Run States

Check run states:

- `queued`
- `running`
- `passed`
- `failed`
- `cancelled`
- `timed_out`
- `stale`

Rules:

- checks are tied to exact snapshots
- a check becomes `stale` when its snapshot is no longer the changeset head
- failed checks keep logs and artifacts unless policy requires redaction
- rerunning a check creates a new check run instead of mutating the old result

### Advisory States

Advisory states:

- `reported`
- `triaged`
- `fixing`
- `private_review`
- `release_pending`
- `disclosed`
- `rejected`

Rules:

- advisory-linked changesets default to `embargoed`
- disclosure requires release policy evaluation
- rejected advisories keep private audit state
- backports are separate changesets linked to the same advisory

### Export States

Export states:

- `planned`
- `policy_blocked`
- `ready`
- `exported`
- `failed`
- `revoked`

Rules:

- export operates on approved changeset snapshots
- export is deterministic for the same changeset, target, actor, and export options
- failed exports record the external error and any partial Git refs
- revoked exports record a compensating action, not deletion of history

## Workspace Semantics

### Creation

A workspace is created from a snapshot or target.

```sh
tri workspace create <project> --from main --task "fix failing tests"
```

Creation records:

- base snapshot
- owner actor
- task
- visibility
- policy grants
- materialization preference

### Mutation

Agents mutate through APIs:

```sh
tri files read <workspace> package.json
tri files read <snapshot> package.json
tri files write <workspace> src/foo.ts --body ./foo.ts
tri files delete <workspace> src/old.ts --expect-head <snapshot>
tri files move <workspace> src/a.ts src/b.ts --expect-head <snapshot>
```

Each mutation:

1. validates policy
2. writes blobs and tree deltas
3. updates the workspace overlay
4. creates a new head snapshot
5. appends operation and event records

### Command Boundaries

Command execution is part of workspace history.

```sh
tri run <workspace> -- npm test
```

At command start and finish, Tri records:

- snapshot before command
- snapshot after command
- command
- environment
- logs
- artifacts
- secret leases
- exit status
- actor
- cost

If the command modifies files, those file changes become operations, not invisible filesystem side effects.

### Updating A Workspace

A workspace can update its base target without becoming a branch.

```sh
tri workspace update <workspace> --from main --expect-head <snapshot>
```

Tri computes:

- old base to new base changes
- old base to workspace head changes
- merge plan
- conflicts
- new base snapshot
- new head snapshot, if merge succeeds

Conflicts are structured objects, not untracked merge-marker text.

### Materialization

A workspace can be materialized for tools that need files:

```sh
tri workspace mount <workspace> ./workspaces/fix-tests
```

Materialization is a cache. If the directory is deleted, Tri can recreate it from snapshots and operations.

## Changeset Semantics

### Creation

A changeset is created from a workspace:

```sh
tri changeset create <workspace> --title "Fix failing tests"
```

Tri records:

- base snapshot
- head snapshot
- selected operations
- task run
- touched paths
- policy impact
- check runs
- suggested reviewers
- export preview

### Splitting

Changesets should be splittable after work completes.

Split dimensions:

- paths
- operation ranges
- generated vs authored files
- dependency updates
- policy changes
- test-only changes
- package boundaries

The first MVP can support path-based and operation-range splitting. Semantic splitting can come later.

### Stacking

A changeset can depend on another changeset without pretending that dependency is a branch.

Stack fields:

- parent changeset
- base snapshot
- dependency state
- revalidation checks

### Conflict Objects

Conflict kinds:

- file text conflict
- binary conflict
- delete/modify conflict
- rename/rename conflict
- path visibility conflict
- secret export conflict
- policy conflict
- generated artifact conflict

Each conflict includes:

- conflicting objects
- involved actors
- involved operations
- proposed resolutions
- required policy checks

Resolution is recorded as an operation.

## Policy And Visibility

Repo-level public/private is too coarse.

Tri policy should support:

- public source with private draft work
- public source with private subpackages
- private changesets in public projects
- embargoed advisory work
- maintainer-only review
- generated secret config that cannot export
- check logs with redacted sections
- artifacts visible only to trusted actors

### Visibility Labels

Initial labels:

- public
- private
- maintainer
- task-only
- review-only
- embargoed
- non-exportable

Labels are not enough by themselves. They are inputs to policy decisions.

### Policy Evaluation Points

Tri evaluates policy when an actor:

- reads a file
- lists a tree
- searches code
- views a diff
- creates a workspace
- writes a file
- runs a command
- requests a secret
- opens a review
- exports a patch
- publishes a release

### Redacted Views

When policy allows partial access, Tri should return redacted structured views instead of failing the whole request.

Examples:

- show public files but hide private package files
- show that a private changeset exists without showing its diff
- show check status but hide secret-bearing logs
- show a redacted advisory until disclosure

### Policy v0

The MVP policy language should be deliberately small. It should be expressive enough to prove object-level visibility and secret export safety without becoming a general policy engine.

Policy v0 is a list of ordered rules.

```json
{
  "schema": "tri.policy.v0",
  "schema_version": "0.1.0",
  "id": "policy_project_default",
  "scope": {
    "project": "proj_123"
  },
  "rules": [
    {
      "id": "public_source_read",
      "effect": "allow",
      "actions": ["file.read", "tree.list", "diff.view"],
      "actors": ["role:anyone"],
      "objects": [{"visibility": "public"}]
    },
    {
      "id": "private_changeset_maintainers",
      "effect": "allow",
      "actions": ["changeset.read", "review.read"],
      "actors": ["role:maintainer"],
      "objects": [{"visibility": "private"}]
    },
    {
      "id": "no_secret_export",
      "effect": "deny",
      "actions": ["export.git_patch", "export.git_commit"],
      "actors": ["role:anyone"],
      "objects": [{"labels": ["non-exportable"]}],
      "reason": "secret_export_blocked"
    },
    {
      "id": "no_hidden_path_export",
      "effect": "deny",
      "actions": ["export.git_patch", "export.git_commit", "export.github_pr"],
      "actors": ["role:anyone"],
      "objects": [{"redaction": "hidden_path"}],
      "reason": "visibility_export_blocked"
    }
  ]
}
```

Policy v0 rule fields:

- `id`
- `effect`: `allow` or `deny`
- `actions`
- `actors`
- `objects`
- `conditions`, optional
- `reason`, required for deny rules

Evaluation rules:

- deny wins over allow
- exact object policy wins over inherited project policy
- missing allow means deny
- policy decisions are recorded as events
- redaction is a policy outcome, not a string filter applied after the fact

Initial action names:

- `file.read`
- `file.write`
- `tree.list`
- `diff.view`
- `workspace.create`
- `workspace.mutate`
- `command.run`
- `secret.lease`
- `config.materialize`
- `target.refresh`
- `check_set.create`
- `changeset.read`
- `changeset.create`
- `review.read`
- `review.write`
- `check.read`
- `artifact.read`
- `export.git_patch`
- `export.git_commit`
- `release.publish`
- `advisory.read`
- `advisory.disclose`

Initial actor selectors:

- `actor:<id>`
- `role:anyone`
- `role:maintainer`
- `role:security`
- `role:agent`
- `task:<id>`
- `owner:<object>`

Initial object selectors:

- exact object id
- project id
- path prefix
- path glob
- package id
- visibility label
- object type
- object labels

Conditions should stay minimal:

- `task == <id>`
- `workspace == <id>`
- `check == <id>`
- `time < embargo_until`
- `required_checks_passed == true`
- `review_approved == true`

The v0 engine should prefer explicit denials over clever inference. If a rule cannot be evaluated, the result is deny with a structured diagnostic.

Project-wide mutations such as target refreshes and reusable check-set creation are maintainer operations. Agents can create workspaces, run task attempts, and propose changes without being able to move shared targets or rewrite the project's required-check vocabulary.

## Secrets And Config

Tri should make `.env` sharing obsolete.

### Secret Rules

- Secrets are stored in a provider or encrypted secret store, not in source snapshots.
- Workspaces request secret leases through policy.
- Secret metadata reads require `secret.read`; secret lease creation requires `secret.use`.
- Lease revocation requires the lease actor, a workspace writer, or maintainer authority.
- Check runs receive only scoped leases.
- Logs are redacted by default.
- Generated config files are marked non-exportable.
- Diffs and exports fail closed when secret-bearing files appear.

### Generated Config

Agents should ask Tri for config:

```sh
tri config materialize <workspace> --profile test
```

Tri can generate a local `.env`-like file for legacy tools, but the file is a workspace artifact with export restrictions.

## Threat Model

Tri is source control for agents, so the threat model includes mistakes by authorized automation, not only malicious attackers.

### Security Goals

Tri should protect:

- private source content in public projects
- embargoed security changes
- secret values and generated config
- agent transcripts, prompts, logs, and artifacts
- policy decisions and audit evidence
- export boundaries into Git and hosted forges
- workspace integrity under concurrent agents

### Non-Goals For MVP Security

The MVP does not need to solve:

- hostile kernel or filesystem compromise
- fully untrusted code execution sandboxing
- distributed consensus between Tri servers
- cryptographic transparency logs
- production-grade secret manager replacement
- malicious maintainer governance

Those can be future work. The MVP still needs fail-closed policy behavior and safe defaults.

### Threats And Required Mitigations

#### Secret Leakage Into Source

Threat: an agent writes `.env`, tokens, credentials, or generated config into source.

Mitigations:

- generated config artifacts are `non-exportable`
- exports evaluate policy over all touched paths and artifacts
- known secret-like paths are denied by default
- check logs are redacted before storage when they contain leased secret values
- secret leases are referenced by id, never serialized as source blobs

#### Private Changes Leaking Through Diffs

Threat: private files, hidden packages, or embargoed changes appear in public diffs or search.

Mitigations:

- diff generation runs policy before producing hunks
- redacted paths are explicit in diff output
- search results carry policy decisions
- exports fail when required paths are denied or redacted

#### Agent Overreach

Threat: an agent reads or mutates objects outside its task or permission scope.

Mitigations:

- every file read, write, diff, command, secret lease, and export evaluates actor policy
- task-scoped actors can only access task-scoped workspaces and changesets
- policy decisions are recorded as events
- unknown actors fail closed

#### Shared Workspace Corruption

Threat: concurrent agents overwrite each other's work.

Mitigations:

- workspace head updates use compare-and-swap
- stale writes fail with structured diagnostics
- separate agents should use separate workspaces by default
- reconciliation produces conflict objects, not corrupted state

#### Materialized Directory Escape

Threat: a materialized workspace follows symlinks or paths outside the mount root.

Mitigations:

- normalized Tri paths reject absolute and parent segments
- materializers reject writes escaping the mount root
- symlink targets are stored but not followed during tree hashing
- generated config lives under tracked artifact policy

#### Unreviewed Export

Threat: an agent exports a changeset before required review, checks, or advisory policy.

Mitigations:

- export is a policy-evaluated action
- export pins the approved changeset snapshot
- missing checks or review approvals block export
- local override requires an explicit event with actor identity and reason

#### Audit Tampering

Threat: an actor deletes or rewrites event history after unsafe behavior.

Mitigations:

- event log is append-only at the API level
- events include input and output fingerprints
- event reads are policy filtered and redact hidden path refs
- destructive administrative repair writes compensating events
- exported artifacts include source event ranges

### Default Deny Rules

The first implementation should deny these by default:

- exporting files matching `.env`, `.env.*`, `*.pem`, `*.key`, and common credential paths
- exporting generated config artifacts
- reading embargoed changesets without `role:security` or explicit grant
- mutating archived, discarded, or promoted workspaces
- exporting changesets with missing required checks
- exporting changesets with unresolved conflicts
- running commands with broad secret access

### Design Risks

The hardest risks are product and architecture risks, not just security bugs.

- If Tri becomes a Git wrapper, GitButler will already be better.
- If Tri only records sessions, Entire will already be better.
- If policies are too complex, agents will not reason about them reliably.
- If materialization is slow or fragile, agents will fall back to worktrees.
- If review objects are noisy, humans will prefer PRs.
- If secret handling blocks common workflows too often, users will bypass it.
- If Git export is lossy, teams will not adopt it at the boundary.

## GitHub For Agents

GitHub is a collaboration product built on top of Git.

Tri should become a collaboration product built on top of workspaces, snapshots, changesets, task runs, and policy.

### Tasks Replace Issues As The Work Primitive

Issues are discussion. Tasks are executable specs.

A task can:

- define acceptance criteria
- spawn workspaces
- assign humans or agents
- set required checks
- compare attempts
- promote one or more changesets

### Reviews Replace Pull Requests As The Decision Surface

Pull requests review branches. Tri reviews changesets.

A review object includes:

- code diff
- task prompt
- actor identity
- model/tool identity
- commands run
- check results
- artifacts
- assumptions
- unresolved risks
- policy findings
- visibility impact
- suggested split points
- export preview

Humans should be able to request another agent pass from inside the review without losing context.

### Checks Replace CI Log Scraping

Checks are structured execution records attached to task runs and changesets.

Agents should query:

- what command ran
- what snapshot it ran against
- what secrets it used
- what files changed
- what failed
- what artifacts were produced
- whether the result is reproducible

### Advisories Replace Hidden Security Branches

Security work should not require public issue trackers or public branches before a fix is ready.

Advisories coordinate:

- private reports
- embargoed changesets
- trusted reviewers
- private checks
- backports
- release timing
- public disclosure

### Releases Replace Tags Alone

A release is more than a Git tag.

It should know:

- included changesets
- public/private packages
- passed checks
- generated artifacts
- disclosed advisories
- rollback target
- downstream update requirements

### Search Spans Work, Not Just Default Branch

Search should cover:

- public code
- authorized private code
- historical snapshots
- active workspaces
- changesets
- reviews
- tasks
- check logs
- release artifacts

Search must respect policy and show provenance.

MVP command evidence:

```sh
tri search <project> <query> --include-history
tri search <project> <query> --scope workspaces --scope changesets --scope reviews --scope checks
```

Each match should name the source scope, object id, field or path, snapshot when relevant, and provenance such as workspace, changeset, review action, check log artifact, release, target, or operation snapshot. Unauthorized objects and policy-hidden paths are counted without leaking hidden path names or text.

## API Surface

Tri is API-first. The CLI should be a thin client over the same operations agents use.

### File API

Required operations:

- read file
- write file
- delete file
- move file
- list tree
- stat path
- search
- diff snapshots
- diff workspace

### Workspace API

Required operations:

- create workspace
- list workspaces
- inspect workspace
- update workspace base
- snapshot workspace
- discard workspace
- materialize workspace
- archive workspace

### Task API

Required operations:

- create task
- inspect task
- complete task run
- list task attempts
- block task
- resume task
- accept task with accepted changesets or no-change resolution
- reject task
- close task

### Changeset API

Required operations:

- create changeset
- inspect changeset
- split changeset
- stack changeset
- compare changesets
- merge changeset
- supersede changeset
- export changeset

### Review API

Required operations:

- create review
- add comment
- request changes
- request agent pass
- approve
- mark risk
- attach policy finding
- attach check summary

### Check API

Required operations:

- run command
- list check runs
- inspect check run
- stream stored log chunks
- list artifacts
- compare check runs
- rerun check

### Policy API

Required operations:

- evaluate access
- evaluate export
- explain decision
- list object visibility
- grant actor
- revoke actor
- create embargo
- disclose embargo

### Secret API

Required operations:

- request lease
- revoke lease
- materialize config
- audit access
- redact logs

## CLI Shape

The CLI should be explicit and scriptable.

```sh
tri serve --host 127.0.0.1 --port 8765
tri actors inspect agent:a

tri import ./repo --name my-project
tri packages declare my-project frontend --path "packages/frontend/**" --visibility public
tri packages declare my-project internal --path "packages/internal/**" --visibility private --owner role:maintainer
tri packages list my-project
tri packages inspect my-project frontend
tri targets list my-project
tri targets inspect my-project main

tri checks set-create my-project smoke --required-check "npm test"
tri task create my-project --title "fix failing tests" --acceptance "regression test passes" --check-set smoke
tri task inspect <task>
tri workspace create my-project --from main --task <task> --hide-path "private/**"
tri workspace inspect <workspace>
tri workspace snapshot <workspace> --expect-head <snapshot>
tri workspace pause <workspace>
tri workspace resume <workspace>
tri workspace update <workspace> --from main --expect-head <snapshot>
tri conflicts list --workspace <workspace>
tri conflicts inspect <conflict>
tri conflicts resolve <conflict> --strategy theirs
tri workspace mount <workspace> ./tmp/fix-tests

tri files read <workspace> package.json
tri files read <snapshot> package.json
tri files write <workspace> src/foo.ts --body ./foo.ts
tri files delete <workspace> src/old.ts --expect-head <snapshot>
tri files move <workspace> src/a.ts src/b.ts --expect-head <snapshot>
tri diff <base-snapshot> <head-snapshot> --mode summary --path "src/**"
tri search my-project "checkout" --include-history

tri run <workspace> -- npm test
tri secrets put my-project API_TOKEN --value-file ./token.txt
tri secrets inspect <secret> --project my-project
tri secrets request-lease <workspace> API_TOKEN --env API_TOKEN
tri config materialize <workspace> --profile test
tri run <workspace> --with-lease <lease> -- npm test
tri secrets redact-logs <check-run> --lease <lease>
tri secrets audit --workspace <workspace>
tri secrets revoke <lease>
tri leases inspect <lease>
tri checks list <workspace>
tri checks list <changeset>
tri checks inspect <check-run>
tri checks stream <check-run> --log stdout --offset 0 --limit 4096
tri checks rerun <check-run>
tri checks compare <check-run-a> <check-run-b>
tri task run-inspect <task-run>
tri task complete-run <task-run> --recommendation "Use this changeset."
tri task block <task> --reason "missing repro"
tri task resume <task> --reason "repro added"
tri task accept <task> --changeset <changeset>
tri task reject <task> --reason "wrong goal"
tri task close <task> --reason "shipped"

tri diff <workspace>
tri changeset create <workspace> --title "Fix failing tests" --hide-path "security/**"
tri changeset inspect <changeset>
tri changeset split <changeset> --paths "src/**"
tri changeset split <changeset> --operations "1..3" --apply --title "Extract focused fix"
tri changeset stack <child-changeset> --after <parent-changeset>
tri changeset revalidate <changeset>
tri changeset ready <changeset> --reason "addressed review"
tri changeset supersede <changeset> --by <replacement-changeset>
tri changeset discard <changeset> --reason "wrong approach"
tri policy evaluate access <changeset> --action changeset.read
tri policy explain <decision>
tri policy create-embargo <project> --title "Auth bypass"
tri policy disclose-embargo <project> --include <changeset> --advisory <advisory>
tri policy visibility <changeset>
tri policy grant <changeset> --principal actor:agent:b --action changeset.read
tri policy grant-inspect <grant>
tri policy revoke <grant>
tri review create <changeset>
tri review comment <review> --path src/app.ts --line 12 --body "why this branch?"
tri review action-inspect <review-action>
tri review comment <review> --reply-to <comment-action> --body "because the legacy parser needs it"
tri review resolve-thread <review> --thread <comment-action>
tri review comment <review> --body "looks small"
tri review request-changes <review> --body "add a regression test"
tri review attach-check-summary <review> --body "checks refreshed"
tri review attach-policy-finding <review> --body "export policy snapshot"
tri review close <review> --body "superseded by another attempt"
tri review approve <review>
tri changeset merge <changeset> --target main --retry 5
tri review request-agent-pass <review> --task "minimize the patch"

tri merge-rules add my-project docs --path "docs/**" --max-files 20
tri merge-rules list my-project
tri files log <workspace-changeset-target-or-snapshot> src/app.ts
tri files blame <workspace-changeset-target-or-snapshot> src/app.ts
tri snapshots merge-base <workspace> <target>

tri auth issue agent:a --file ./tokens.json --label ci
tri serve --host 0.0.0.0 --port 8765 --auth-file ./tokens.json
tri --actor agent:claude mcp

tri policy evaluate export <changeset>
tri artifacts list --kind stdout
tri export git-patch <changeset>
tri export git-commit <changeset>
tri export github-pr <changeset> --repo ./repo --branch tri/fix-tests --base main
tri export inspect <export>
tri export list my-project --changeset <changeset>
```

## Local HTTP API Shape

`tri serve` should expose the same policy-gated command behavior over HTTP for agents that operate better with stable URLs than shell argv.

Required initial routes:

- `GET /v0/health`
- `GET /v0/commands`
- `GET /v0/schema` and `GET /v0/schema/<schema>`
- `POST /v0/command` with `{"actor": "agent:id", "argv": [...]}`
- `GET /v0/objects/<object>`
- `GET /v0/actors/<actor>`
- `GET /v0/projects/<project>/packages`
- `GET /v0/projects/<project>/packages/<package>`
- `GET /v0/packages/<package>`
- `GET /v0/tasks/<task>`
- `GET /v0/task-runs/<task-run>`
- `GET /v0/tasks/<task>/attempts`
- `GET /v0/check-sets/<check-set>`
- `GET /v0/targets/<target>`
- `GET /v0/workspaces/<workspace>`
- `GET /v0/workspaces/<workspace>/files`
- `GET /v0/workspaces/<workspace>/files/<path>`
- `GET /v0/workspaces/<workspace>/conflicts?status=open`
- `GET /v0/leases/<lease>`
- `GET /v0/snapshots/<snapshot>/files`
- `GET /v0/snapshots/<snapshot>/files/<path>`
- `GET /v0/trees/<tree>`
- `GET /v0/blobs/<blob>`
- `GET /v0/workspaces/<workspace>/diff?mode=summary&path=src/**`
- `GET /v0/changesets/<changeset>`
- `GET /v0/changesets/<changeset>/checks`
- `GET /v0/changesets/<changeset>/diff?mode=patch&path=src/app.ts`
- `GET /v0/reviews/<review>`
- `GET /v0/review-actions/<review-action>`
- `GET /v0/checks/<check>`
- `GET /v0/checks/<check>/artifacts`
- `GET /v0/checks/<check>/logs/stdout?offset=0&limit=4096`
- `GET /v0/conflicts?workspace=<workspace>&status=open`
- `GET /v0/conflicts/<conflict>`
- `GET /v0/grants/<grant>`
- `GET /v0/secrets/<secret>`
- `GET /v0/events/<event>`
- `GET /v0/exports/<export>`
- `GET /v0/releases/<release>`
- `GET /v0/advisories/<advisory>`
- `GET /v0/artifacts/<artifact>` and `GET /v0/artifacts/<artifact>/content`
- `GET /v0/policy/decisions/<decision>`

All GET routes accept `X-Tri-Actor` and must use the same policy checks as the CLI command they wrap.

When the server runs with `--auth-file`, every route except health requires `Authorization: Bearer <token>`. The token decides the actor. A request that names a different actor through `X-Tri-Actor`, the payload, a batch entry, or `--actor` fails with `actor_mismatch`; a missing or revoked token fails with HTTP 401 and `unauthenticated`.

### MCP Shape

`tri mcp` speaks the Model Context Protocol over stdio (protocol versions 2025-06-18, 2025-03-26, and 2024-11-05). It exposes two tools:

- `tri`: `{"argv": [...], "response": "full" | "summary"}` runs one command and returns the Tri response envelope as both text and `structuredContent`, with `isError` set when `ok` is false
- `tri_batch`: the same plan format as `tri batch`, run in one round trip

The server runs every call as the actor it started with. Start it as a specific agent, for example `tri --actor agent:claude mcp`; starting it as `actor_local` hands the model maintainer rights.

## Git Compatibility

Git compatibility is necessary, but it is boundary behavior.

### Import

On import, Tri should map:

- Git blobs to Tri blobs
- Git trees to Tri trees
- Git commits to Tri snapshots
- Git refs to Tri targets
- Git authors to actor references when possible

Imported Git history can be preserved without making commits the authoring primitive.

### Export

Tri should export:

- unified diff patches
- Git commits
- Git branches, if needed by a remote
- GitHub pull requests, if needed by a team

Export records:

- source changeset
- exported commit ids
- exported actor identity
- policy decision
- export timestamp
- target remote

### Mirror Mode

Tri must mirror a Git remote bidirectionally. It was deferred for the first proof; it is now an adoption requirement, because Cursor Origin and Zed DeltaDB both show that teams adopt a new system only when teammates who never install it still see an ordinary Git repository. Merged Tri targets appear as commits on the mirrored branch, and pushes to that branch land as Tri snapshots.

`tri mirror` implements this. Each mirror keeps a private bare repository under the Tri home and a synced pair: one Tri snapshot and one Git commit whose mirrorable trees are identical. A sync:

1. fetches the branch and turns each first-parent commit after the pair into a snapshot attributed to the Git author (`human:<email>`), with `git_commit` metadata so `files blame` and `files log` cite it
2. fast-forwards the target when only Git moved, or three-way merges the Git tip with the target (line-level, against the synced snapshot) when both moved
3. writes one Git commit per Tri target move since the pair (titled by the changeset, with `Tri-Changeset` and `Tri-Snapshot` trailers), plus a merge commit when both sides moved, and pushes without force
4. records the new pair

A path is mirrorable when it is not secret-like and the mirror's audience (default `service:git-mirror`) can read it. Git never receives Tri's copy of a non-mirrorable path and Tri never takes Git's, so a public mirror of a project with private packages stays public-safe in both directions. A branch that already differs from the target needs `--prefer git` or `--prefer tri` the first time. Force pushes are mirrored as one change with a `mirror_history_rewritten` warning.

## First Implementation

Build a local daemon and CLI first.

### Components

Required local components:

- `tri serve`: JSON API server and coordinator, with bearer-token actors for anything beyond loopback
- `tri mcp`: MCP stdio server for agent harnesses
- content store: blobs, trees, snapshots, artifacts
- metadata DB: projects, workspaces, changesets, reviews, checks, policies
- event log: append-only actor and operation history
- Git adapter: import and export
- workspace materializer: optional filesystem cache
- runner: local command execution with structured logs
- policy engine: path/object visibility and export rules
- secret stub: scoped generated config without raw source storage

### Storage Choice

For the MVP:

- content objects on disk under `.tri/objects` or `~/.tri/objects`
- SQLite for metadata and event log
- JSON output for every CLI command
- BLAKE3 or SHA-256 content hashes
- deterministic tree hashing with sorted normalized paths

The exact hash is less important than committing to content addressing and deterministic tree identity.

### MVP Tables

The local metadata store likely needs:

- projects
- targets
- blobs
- trees
- snapshots
- workspaces
- workspace_operations
- events
- tasks
- task_runs
- changesets
- reviews
- check_runs
- artifacts
- policies
- actors
- secret_refs
- exports

This is enough to prove the model before adding a hosted service.

### MVP Required Features

Required:

- import a Git repo
- create workspaces from imported targets
- read, write, list, and diff files through the API
- automatically snapshot every mutation
- record command runs as structured check runs
- create changesets from workspaces
- create review objects from changesets
- enforce path-level visibility on files and changesets
- provide generated config for a check without storing it in source history
- export a changeset as a Git patch, commit, or local GitHub PR request

Not required:

- hosted UI
- distributed sync
- semantic code understanding
- full secret manager integration
- real package publishing
- full GitHub issue migration
- full CI replacement
- hosted forge automation beyond local branch and PR-request export

### Phase Gates

Build the MVP in gates that each produce inspectable evidence.

#### Phase 0: Source Import

Goal: Tri can import a Git repository into its own content model.

Required evidence:

- `tri import` creates project, target, blob, tree, and snapshot records.
- Imported Git commit tree hash maps to a Tri snapshot fingerprint.
- `tri files list <snapshot>` matches `git ls-tree` for the imported ref.
- No workspace or changeset features are required yet.

#### Phase 1: Workspace Mutations

Goal: an agent can edit code without using Git.

Required evidence:

- `tri workspace create` creates an isolated workspace from a target.
- `tri workspace pause` and `tri workspace resume` enforce readable-but-non-mutable paused workspaces.
- `tri files read` works against workspaces and snapshots.
- `tri files write/delete/move` works against the workspace.
- Every mutation creates a new snapshot, operation, and event.
- Stale workspace writes fail with a structured diagnostic.
- `tri diff <workspace>` is reproducible from snapshots.
- `tri diff <base-snapshot> <head-snapshot>` compares explicit snapshots through the same policy-aware diff surface.

This is the first point where Tri must feel meaningfully different from Git.

#### Phase 2: Materialization And Checks

Goal: legacy tools can run without making the filesystem authoritative.

Required evidence:

- `tri workspace mount` materializes a workspace into a directory.
- `tri workspace mount` creates an inspectable materialization lease with path, owner, ignored paths, mount snapshot, last sync snapshot, and expiry.
- `tri workspace mount` materializes archived workspaces as read-only leases that cannot sync back.
- `tri workspace sync` refuses stale materializations if the workspace head moved independently.
- `tri workspace sync --lease <lease>` imports filesystem edits through a specific active materialization lease.
- `tri run` executes a command and records a check run.
- `tri run --assumption <text>` records agent assumptions on the check run and its task run.
- File changes made by the command are imported back as operations.
- Logs and artifacts are addressable objects.
- `tri task run-inspect` exposes the run's checks, commands, log/artifact ids, resulting changesets, assumptions, and aggregate cost without requiring agents to scrape review output.
- The check run records before/after snapshots, materialized-directory fingerprints, and environment policy.
- Task-required checks can be declared up front inline or through reusable named check sets, and must pass at the changeset head.
- Completed task runs record a final recommendation.
- `tri task block/resume/accept/reject/close` records explicit task lifecycle state and resolution evidence.

This is the first point where Tri becomes useful for real repos.

#### Phase 3: Changesets And Review

Goal: work becomes reviewable without creating a branch.

Required evidence:

- `tri changeset create` pins base and head snapshots.
- A changeset includes selected operations, touched paths, checks, and provenance.
- `tri changeset split` works by path or operation range.
- `tri changeset stack` records parent dependencies without creating branches.
- `tri changeset revalidate` reruns selected checks against the changeset head snapshot without creating a branch or mutating the workspace.
- `tri changeset ready` moves a changes-requested changeset back to ready and reopens changes-requested reviews before approval.
- `tri changeset discard` and `tri changeset supersede` retire changesets without deleting audit history, and superseded changesets point to their replacement.
- `tri review create` produces a review object with check summary and export preview.
- `tri review inspect` exposes task context, commands, check results, artifacts, policy findings, visibility impact, and export preview without requiring raw log scraping.
- `tri review attach-check-summary` and `tri review attach-policy-finding` persist review evidence as explicit actions agents can cite.
- `tri review comment` supports path/line anchored threads, replies, and explicit thread resolution.
- `tri review close` closes the review without deleting or hiding the changeset, and closed reviews reject further mutation.
- Assigned reviewer approval tracks approvals and pending reviewers, and rejects unassigned non-maintainer approval.
- Direct artifact listing, inspection, and reads enforce the owning workspace, changeset, release, export, or event policy.
- `tri changeset merge` can promote approved work into a Tri target without creating a Git branch.
- A stacked child changeset cannot merge or export until its parent has merged.
- If the target moved, Tri either creates a merge snapshot or returns structured conflicts.

This is the first point where Tri can challenge GitButler's branch-centered model.

#### Phase 4: Policy And Secrets

Goal: privacy and `.env` handling are part of source control, not conventions.

Required evidence:

- Path-level visibility can hide files from diff and review output.
- Local Policy v0 can express hidden path globs with `--hide-path`.
- Package boundaries can express public or private package globs, and private package paths are hidden automatically for actors without `package.read`.
- Local Policy v0 can grant and revoke actor/role access to private workspaces and changesets.
- Local Policy v0 can evaluate object access, explain visibility and grants to actors with object read access, and inherit workspace read grants to derived changesets and reviews.
- `tri policy explain` retrieves a recorded allow or deny policy decision by id without requiring agents to scrape event logs. Object, task-run, event, check, and artifact access `policy_denied` diagnostics include the decision id, event id, and explain command, and denied decisions against changesets are included in the changeset policy-decision trail for audit.
- `tri policy create-embargo` and `tri policy disclose-embargo` expose advisory embargo lifecycle through the Policy API.
- A public project can contain a private changeset.
- `tri advisory create --embargoed` creates an advisory coordination object.
- Advisory-linked workspaces and changesets default to `embargoed`.
- `tri workspace create --advisory <advisory>` requires advisory read access before attaching the workspace.
- Advisory-linked changesets cannot merge or export until `tri release prepare --disclose` evaluates policy and records coordinated disclosure.
- `tri config materialize` creates a non-exportable generated config artifact and links it to the active materialization lease when one exists.
- `tri secrets list`, `tri secrets request-lease`, and `tri secrets revoke` are policy-gated.
- `tri secrets redact-logs` can reprocess stored check logs against secret leases and records a redaction event.
- `tri export` refuses to export secret-like files, policy-hidden files, or attached non-exportable artifacts.
- Policy decisions are recorded and explainable.

This is the first point where Tri can challenge Entire's redaction-centered model.

#### Phase 5: Git Export

Goal: accepted Tri work can leave through normal Git.

Required evidence:

- `tri export git-patch <changeset>` creates a valid patch.
- `tri export git-commit <changeset>` creates a valid commit in a temporary Git checkout or target repo.
- `tri export github-pr <changeset>` prepares a Git branch and PR request artifact, and can call a configured forge CLI.
- Successful exports advance the changeset lifecycle to `exported` and record the export id in lifecycle metadata.
- `tri export inspect` and `tri export list` expose export records to agents.
- Export records include actor, changeset, policy decision, Git object ids, diagnostics, source event ranges, and re-export determinism metadata.
- Re-exporting the same reviewed changeset is deterministic for content-addressed patch exports or explains why external Git/forge state prevents deterministic replay.

### Evaluation Matrix

The MVP should be judged by behaviors, not positioning.

| Workflow | Git Worktrees + GitHub | Entire | GitButler | Scion (internal) | Tri Requirement |
| --- | --- | --- | --- | --- | --- |
| Parallel agent attempts | manual worktrees and branches | tracked sessions, still Git workspace based | parallel branch lanes in one working directory | server-side changesets, sparse checkouts | isolated copy-on-write workspaces |
| Context before commit | shell history and agent logs | session capture, checkpointed around commits | branch agent session context | not described | event log from first mutation |
| Review unit | pull request branch | Trail on each push | branch or stacked PR | changeset reviewed in Antique | changeset with operations and checks |
| Merge throughput | merge queue | forge merge | forge merge | no global lock, tests after merge | line merges, merge rules, compare-and-swap retries |
| Private draft in public project | private fork or separate repo | private checkpoint remote, if configured | private branch/remote conventions | per-directory permissions | object-level visibility |
| Secrets/config | `.env` conventions and secret scanners | transcript redaction and ignored files | Git/forge conventions | not described | secret leases and non-exportable config artifacts |
| Check evidence | CI logs | agent scores and session transcript | forge CI status and local operation history | server-side diff and blame for CI | structured check-run objects |
| Agent API surface | shell plus Git porcelain | hooks capture agent sessions | hooks and `but mcp` organize branch output | `scn` client and RPCs | typed commands over CLI, authenticated HTTP, and MCP |
| History queries | full clone | full clone | full clone | server-side blame, history, merge base | `files log`, `files blame`, `snapshots merge-base` on the store |
| Export | native Git | native Git | native Git | not exported | policy-gated Git adapter and a two-way mirror (`tri mirror`) |

## Example Workflows

### Agent Fixes A Bug

```sh
tri import ./repo --name app
tri task create app --title "Fix failing checkout test"
tri workspace create app --from main --task <task>
tri files read <workspace> src/checkout.ts
tri files write <workspace> src/checkout.ts --body ./candidate.ts
tri run <workspace> --assumption "checkout fixture matches production rounding mode" -- npm test -- checkout
tri changeset create <workspace> --title "Fix checkout total rounding"
tri review create <changeset>
tri review approve <review>
tri changeset merge <changeset> --target main
tri policy evaluate export <changeset>
tri export git-patch <changeset>
```

### Three Agents Compete

```sh
tri workspace create app --from main --task <task> --actor agent:a
tri workspace create app --from main --task <task> --actor agent:b
tri workspace create app --from main --task <task> --actor agent:c
tri task attempts <task>
tri changeset compare <changeset-a> <changeset-b> <changeset-c>
```

### Public Project, Private Security Fix

```sh
tri advisory create app --title "Auth bypass" --embargoed
tri workspace create app --from main --advisory <advisory> --visibility embargoed
tri changeset create <workspace> --visibility embargoed
tri review create <changeset> --reviewers security-team
tri release prepare app --include <changeset> --disclose <advisory>
```

### Check With Scoped Config

```sh
tri config materialize <workspace> --profile test
tri secrets request-lease <workspace> API_TOKEN --env API_TOKEN
tri run <workspace> --with-lease <lease> --assumption "test profile uses seeded API credentials" -- npm test
tri checks inspect <check-run>
```

The generated config is an artifact with a non-exportable policy, and the secret value is only exposed to the check process through a lease. Neither becomes a source file.

## Success Criteria

Tri is worth continuing only if the MVP proves these claims with actual command output:

- Three workspaces can start from the same base without branch or checkout conflicts.
- An agent can complete a file edit flow using only Tri file APIs.
- Every mutation produces a snapshot and event record.
- A mounted filesystem cache cannot sync over a newer workspace head.
- A mounted filesystem cache has an inspectable materialization lease that can be listed, inspected, revoked, and used as the explicit sync authority.
- Workspace lifecycle state is enforced: paused workspaces remain readable but reject mutation until resumed, discarded work cannot merge/export, promoted workspaces reject mutation, and archived workspaces can mount read-only.
- A command run records snapshot, command, logs, artifacts, status, actor, cost, and assumptions.
- A task can carry acceptance criteria, inline or named-set required checks, final task-run recommendation, and lifecycle metadata for blocked/resumed/accepted/rejected/closed states.
- A workspace can become a changeset with checks and provenance attached.
- A changeset can be split after the fact.
- A changeset can depend on another changeset without becoming a branch.
- A stale changeset check can be revalidated at the changeset head without branch state.
- A changes-requested changeset must be marked ready before approval resumes.
- Discarded and superseded changesets remain inspectable, expose lifecycle metadata, and cannot merge or export.
- An approved changeset can merge into a Tri target without Git branch state.
- A successfully exported changeset moves to `exported` and exposes the export id in lifecycle metadata.
- A public project can hold a private changeset.
- Private workspaces and changesets deny other agents until a grant is added.
- Policy access and visibility decisions are machine-readable, include matched grants, and expose workspace-read inheritance to derived changesets and reviews.
- Hidden path policy redacts file listing, search, diff, review, and changeset outputs before export.
- Project search spans authorized files and Tri objects, includes provenance, can include workspace history, and counts policy-denied objects without leaking hidden content.
- A check can use scoped config without writing secrets into source history.
- Stored check logs can be explicitly reprocessed through `tri secrets redact-logs`, with redaction events in the secret audit trail.
- Generated config artifacts are non-exportable, appear on active materialization leases, and block changeset export while attached.
- A changeset can export to a normal Git patch, commit, or PR request artifact.
- Export records are listable and inspectable, with policy decisions, external object ids, source event ranges, and deterministic re-export metadata.
- A reviewer can inspect task context, command evidence, artifacts, policy findings, and visibility impact without reading raw shell history or CI logs.
- A reviewer can attach check summaries and policy findings as durable review actions.
- Direct artifact listing, inspection, and reading are policy-gated by the artifact's owning Tri object.
- Assigned reviewers can approve incrementally, and pending reviewer state is machine-readable.
- Review comment threads expose path/line anchors, replies, and resolution state.
- Closed reviews remain inspectable, do not delete their changeset, and reject further review mutation.
- Advisory-linked changesets default to embargoed and cannot export until a release disclosure is prepared.
- Two changesets that edit disjoint lines of the same text file both merge into a target without a conflict object.
- A maintainer merge rule lets a checked changeset that only touches matching paths merge without a human review, while checks, secrets, hidden paths, and embargoes still block it.
- Concurrent merges into one target retry the compare-and-swap instead of failing back to the agent.
- Path history, line blame, and merge bases are answered from the store without a checkout, and stop at snapshots the actor cannot read.
- An API client holding a bearer token can act only as that token's actor.
- An MCP client can do the full workspace-to-merge loop through the `tri` tool without being able to change its actor.

Competitive success requires narrower proof against Entire and GitButler:

- Compared with Entire, Tri must preserve useful agent context before any Git commit exists.
- Compared with Entire, Tri must protect private work through object policy, not only through redacted checkpoint branches.
- Compared with GitButler, Tri must run parallel agents in isolated workspaces, not one shared working directory with branch lanes.
- Compared with GitButler, Tri must review and export changesets without making branches the internal primitive.
- Compared with GitButler's hooks and MCP server, Tri must let agents do source-control work through Tri APIs before changes hit a shared working directory.
- Compared with both, Tri must expose a structured API surface that an agent can use without a shell-oriented Git workflow.

## Relationship To One

Tri and One are related but separate experiments.

One asks:

```text
What should a programming language look like when agents are primary authors and operators?
```

Tri asks:

```text
What should source control and collaboration look like when agents are primary contributors?
```

Tri should work for normal TypeScript, Python, Rust, Go, and mixed-language repos first. It should not depend on One.

One can later become a strong fit for:

- Tri policy definitions
- agent procedures
- semantic code facts
- proof obligations
- task acceptance criteria
- deployment and rollback rules

The clean split:

```text
Tri owns software change.
One owns software meaning.
```

## Open Questions

Important questions to defer until the MVP pressure-tests the model:

- ~~Should Tri support decentralized sync, or start as a client/server system?~~ Settled 2026-11-20: client/server with one authoritative service and disks as caches, following Scion's evidence that agents are always-online clients. See [Agent-Scale Gaps](#agent-scale-gaps).
- How much semantic diffing is necessary before path/text diffs are insufficient?
- Should policies be declarative JSON, Rego-like rules, One declarations, or a small Tri-native policy language?
- How should large binaries and generated artifacts be stored and garbage-collected?
- How should hosted identity map to local actor identity?
- How should Tri represent submodules, vendored dependencies, and generated lockfiles?
- How much Git history should be imported by default?
- What is the right review UI once the CLI proves the object model?

## Architectural Bet

The bet is not that Git is useless.

The bet is that Git's live model is wrong for agents.

Tri should preserve Git compatibility while replacing the daily primitives:

- workspace instead of checkout
- snapshot instead of manual commit
- changeset instead of branch
- review object instead of pull request page
- task run instead of commit message archaeology
- check object instead of CI log scraping
- policy object instead of repo-level visibility
- secret lease instead of `.env` sharing
- event log instead of missing provenance

If this works, GitHub becomes an integration target instead of the center of the development workflow.
