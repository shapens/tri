from __future__ import annotations

import argparse
import base64
import copy
import difflib
import fnmatch
import hashlib
import http.server
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from socketserver import ThreadingMixIn
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from tri import gitmirror
from tri import sandbox
from tri.auth import TokenAuthority, issue_token, load_token_file, public_record, revoke_tokens
from tri.ignore import IgnoreRules
from tri.textmerge import merge_three_way
from tri.locking import FileLock
from tri.trees import build_tree_nodes, decode_node, flatten as flatten_tree, is_hierarchical
from tri.overlay import OverlayMaterialization, overlay_from_payload, prepare_overlay_run, probe_overlay
from tri.materialize import (
    Attachment,
    CheckoutCache,
    Materialization,
    MaterializeError,
    Materializer,
    clone_dir_supported,
    clone_file,
    load_materialization,
    make_tree_writable,
    probe_clone,
    remove_tree,
    stat_key,
)


SCHEMA_VERSION = "0.1.0"
COMPRESSED_BLOB_MAGIC = b"TRIZ1\0"
BATCH_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")
COMPRESSED_TREE_JSON_PREFIX = "TRIJ1:"
PACKED_DB_MAGIC = b"TRIDB1\0"
PARSER_CACHE: argparse.ArgumentParser | None = None


ACTOR_ACCEPTED_FORMATS = ["actor_local", "agent:<id>", "human:<id>", "service:<id>", "task:<task-id>", "actor_<id>"]


COMMAND_CONTRACTS: list[dict[str, Any]] = [
    {"name": "commands", "usage": "tri commands", "schema": "tri.commands", "description": "List machine-readable command contracts."},
    {"name": "schema", "usage": "tri schema [name]", "schema": "tri.schema", "description": "Show first JSON schemas used by agent clients."},
    {"name": "batch", "usage": "tri batch <plan.json> [--continue-on-error]", "schema": "tri.batch", "description": "Run a dependent JSON command batch in one local agent round trip."},
    {"name": "doctor", "usage": "tri doctor", "schema": "tri.doctor", "description": "Inspect local Tri store health and command availability."},
    {"name": "serve", "usage": "tri serve [--host 127.0.0.1] [--port 8765] [--quiet] [--auth-file <tokens.json>] [--allow-unauthenticated] [--run-sandbox off|auto|required] [--allow-unsandboxed-runs]", "schema": "tri.api.server", "description": "Run the JSON HTTP API; with --auth-file each bearer token is bound to one actor. Beyond loopback, runs are sandboxed by default (--run-sandbox required)."},
    {"name": "auth-issue", "usage": "tri auth issue <actor> --file <tokens.json> [--label <text>]", "schema": "tri.auth.token", "description": "Issue a bearer token bound to one actor; the token is shown once and only its hash is stored."},
    {"name": "auth-list", "usage": "tri auth list --file <tokens.json>", "schema": "tri.auth.list", "description": "List issued tokens without their secrets."},
    {"name": "auth-revoke", "usage": "tri auth revoke <token-id-or-actor> --file <tokens.json>", "schema": "tri.auth.list", "description": "Revoke tokens; a running server stops accepting them on its next request."},
    {"name": "mcp", "usage": "tri mcp [--run-sandbox off|auto|required]", "schema": "tri.mcp.server", "description": "Serve Tri to agent harnesses over the Model Context Protocol (stdio); every call runs as the startup actor."},
    {"name": "objects-inspect", "usage": "tri objects inspect <object>", "schema": "tri.object.inspect", "description": "Inspect any supported typed Tri object id through its policy-aware command."},
    {"name": "actors-inspect", "usage": "tri actors inspect <actor>", "schema": "tri.actor.inspect", "description": "Inspect an actor identity, derived roles, and policy selectors."},
    {"name": "import", "usage": "tri import <repo> --name <name> [--ref <ref>] [--target <target>]", "schema": "tri.import", "description": "Import a Git repo or directory into Tri snapshots."},
    {"name": "projects-list", "usage": "tri projects list", "schema": "tri.project.list", "description": "List projects in the local Tri store."},
    {"name": "projects-inspect", "usage": "tri projects inspect <project>", "schema": "tri.project.inspect", "description": "Inspect project metadata and child object counts."},
    {"name": "gc", "usage": "tri gc [--reap] [--apply] [--retention-days <n>] [--grace-seconds <n>] [--unsafe-no-grace] [--clear-caches]", "schema": "tri.gc", "description": "Plan (default) or apply garbage collection: --reap reaps leases and evicts caches over budget; --apply also purges content no live object references."},
    {"name": "env-define", "usage": "tri env define <project> <name> --install <cmd> --output <path> [--output <path>] [--key-file <glob>] [--key-command <cmd>] [--mode auto|copy|link|private]", "schema": "tri.env.recipe", "description": "Declare a dependency environment built once per key (key files + key commands + platform) and shared read-only by runs and mounts."},
    {"name": "env-list", "usage": "tri env list <project>", "schema": "tri.env.list", "description": "List environment recipes."},
    {"name": "env-inspect", "usage": "tri env inspect <project> <name>", "schema": "tri.env.inspect", "description": "Inspect an environment recipe and its cached builds."},
    {"name": "env-remove", "usage": "tri env remove <project> <name>", "schema": "tri.env.list", "description": "Remove an environment recipe; cached builds age out through GC."},
    {"name": "ignore-add", "usage": "tri ignore add <project> <glob...>", "schema": "tri.ignore.list", "description": "Add project ignore patterns: matching new files are never imported from runs, syncs, or salvage."},
    {"name": "ignore-remove", "usage": "tri ignore remove <project> <glob...>", "schema": "tri.ignore.list", "description": "Remove project ignore patterns."},
    {"name": "ignore-list", "usage": "tri ignore list <project>", "schema": "tri.ignore.list", "description": "List project ignore patterns (applied on top of the snapshot's .gitignore files)."},
    {"name": "packages-declare", "usage": "tri packages declare <project> <name> --path <glob> [--visibility <selector>] [--owner <principal>]", "schema": "tri.package.inspect", "description": "Declare an inspectable package boundary for project policy and splitting."},
    {"name": "packages-list", "usage": "tri packages list <project>", "schema": "tri.package.list", "description": "List project package boundaries."},
    {"name": "packages-inspect", "usage": "tri packages inspect <project> <name-or-id> | <package>", "schema": "tri.package.inspect", "description": "Inspect one project package boundary."},
    {"name": "targets-list", "usage": "tri targets list <project>", "schema": "tri.targets.list", "description": "List project target pointers."},
    {"name": "targets-inspect", "usage": "tri targets inspect <project> <target-or-id> | <target>", "schema": "tri.targets.inspect", "description": "Inspect one project target pointer."},
    {"name": "targets-refresh", "usage": "tri targets refresh <project> <target> [--ref <ref>] [--expect-snapshot <snapshot>]", "schema": "tri.targets.refresh", "description": "Refresh a target from the imported project root."},
    {"name": "snapshots-list", "usage": "tri snapshots list <project>", "schema": "tri.snapshot.list", "description": "List policy-readable snapshots for a project."},
    {"name": "snapshots-merge-base", "usage": "tri snapshots merge-base <ref> <ref>", "schema": "tri.snapshot.merge_base", "description": "Nearest common ancestor snapshot of two workspaces, changesets, targets, or snapshots."},
    {"name": "snapshots-inspect", "usage": "tri snapshots inspect <snapshot>", "schema": "tri.snapshot.inspect", "description": "Inspect immutable snapshot provenance."},
    {"name": "trees-inspect", "usage": "tri trees inspect <tree>", "schema": "tri.tree.inspect", "description": "Inspect a policy-visible content tree and its readable file entries."},
    {"name": "blobs-inspect", "usage": "tri blobs inspect <blob>", "schema": "tri.blob.inspect", "description": "Inspect a policy-visible content blob without reading raw bytes by id."},
    {"name": "task-create", "usage": "tri task create <project> --title <title> [--body <text>] [--body-file <path>] [--acceptance <text>] [--required-check <cmd>] [--check-set <name>]", "schema": "tri.task.inspect", "description": "Create an executable work task."},
    {"name": "task-inspect", "usage": "tri task inspect <task>", "schema": "tri.task.inspect", "description": "Inspect an executable task spec."},
    {"name": "task-run-inspect", "usage": "tri task run-inspect <run>", "schema": "tri.task_run.inspect", "description": "Inspect one task-run attempt."},
    {"name": "task-complete-run", "usage": "tri task complete-run <run> --recommendation <text> [--status completed|failed|cancelled]", "schema": "tri.task_run.inspect", "description": "Complete a task run with a final recommendation."},
    {"name": "task-block", "usage": "tri task block <task> --reason <text>", "schema": "tri.task.lifecycle", "description": "Mark a running task blocked with a machine-readable reason."},
    {"name": "task-resume", "usage": "tri task resume <task> [--reason <text>]", "schema": "tri.task.lifecycle", "description": "Resume a blocked or rejected task."},
    {"name": "task-accept", "usage": "tri task accept <task> (--changeset <changeset> | --no-change <text>)", "schema": "tri.task.lifecycle", "description": "Accept a reviewed task with accepted changesets or a no-change resolution."},
    {"name": "task-reject", "usage": "tri task reject <task> --reason <text>", "schema": "tri.task.lifecycle", "description": "Reject a task in review with a recorded reason."},
    {"name": "task-close", "usage": "tri task close <task> [--reason <text>]", "schema": "tri.task.lifecycle", "description": "Close an accepted or rejected task."},
    {"name": "task-attempts", "usage": "tri task attempts <task>", "schema": "tri.task.attempts", "description": "List workspaces, runs, and changesets for a task."},
    {"name": "workspace-create", "usage": "tri workspace create <project> --from <target> [--task <task-or-title>] [--visibility <selector>] [--advisory <advisory>] [--hide-path <glob>] [--hide-package <pkg>]", "schema": "tri.workspace.create", "description": "Create an isolated workspace from a target."},
    {"name": "workspace-list", "usage": "tri workspace list [project]", "schema": "tri.workspace.list", "description": "List workspaces for a project or local store."},
    {"name": "workspace-inspect", "usage": "tri workspace inspect <workspace>", "schema": "tri.workspace.inspect", "description": "Inspect workspace state."},
    {"name": "workspace-update", "usage": "tri workspace update <workspace> --from <target> [--expect-head <snapshot>]", "schema": "tri.workspace.update", "description": "Merge a workspace onto the current target snapshot."},
    {"name": "workspace-snapshot", "usage": "tri workspace snapshot <workspace> [--expect-head <snapshot>]", "schema": "tri.workspace.snapshot", "description": "Create an explicit workspace checkpoint snapshot."},
    {"name": "workspace-mount", "usage": "tri workspace mount <workspace> <path> [--paths <glob>] [--ttl-seconds <n> | --no-ttl] [--ignore-path <glob>] [--no-env] [--env-link]", "schema": "tri.workspace.mount", "description": "Materialize a workspace into a local directory and create a materialization lease. --paths mounts only matching files; sync keeps the rest of the tree."},
    {"name": "workspace-sync", "usage": "tri workspace sync <workspace> [--lease <lease>]", "schema": "tri.workspace.sync", "description": "Import mounted filesystem edits back into Tri through a materialization lease."},
    {"name": "workspace-leases", "usage": "tri workspace leases <workspace>", "schema": "tri.workspace.materialization_leases", "description": "List materialization leases for a workspace."},
    {"name": "workspace-lease-inspect", "usage": "tri workspace lease-inspect <lease>", "schema": "tri.workspace.materialization_lease", "description": "Inspect a materialization lease."},
    {"name": "workspace-lease-revoke", "usage": "tri workspace lease-revoke <lease>", "schema": "tri.workspace.materialization_lease", "description": "Revoke a materialization lease."},
    {"name": "leases-inspect", "usage": "tri leases inspect <lease>", "schema": "tri.lease.inspect", "description": "Inspect a secret or materialization lease by id."},
    {"name": "leases-list", "usage": "tri leases list [--workspace <workspace>] [--status <status>]", "schema": "tri.lease.list", "description": "List mount leases and run/env-build directory leases with backend and lifecycle state."},
    {"name": "leases-renew", "usage": "tri leases renew <lease> [--ttl-seconds <n>]", "schema": "tri.workspace.materialization_lease", "description": "Extend a mount lease (sync also renews it)."},
    {"name": "leases-reap", "usage": "tri leases reap [<lease> --force]", "schema": "tri.lease.reap", "description": "Reap expired, revoked, discarded, and orphaned directories, salvaging unsynced edits first."},
    {"name": "salvage-list", "usage": "tri salvage list <workspace>", "schema": "tri.salvage.list", "description": "List snapshots salvaged from reaped directories."},
    {"name": "salvage-adopt", "usage": "tri salvage adopt <salvage> [--expect-head <snapshot>]", "schema": "tri.salvage.adopt", "description": "Make a salvage snapshot the workspace head when the head has not moved since."},
    {"name": "workspace-pause", "usage": "tri workspace pause <workspace>", "schema": "tri.workspace.lifecycle", "description": "Pause an active workspace so it remains readable but rejects mutation."},
    {"name": "workspace-resume", "usage": "tri workspace resume <workspace>", "schema": "tri.workspace.lifecycle", "description": "Resume a paused workspace."},
    {"name": "workspace-discard", "usage": "tri workspace discard <workspace>", "schema": "tri.workspace.lifecycle", "description": "Mark an active workspace as discarded."},
    {"name": "workspace-archive", "usage": "tri workspace archive <workspace>", "schema": "tri.workspace.lifecycle", "description": "Archive a workspace after use."},
    {"name": "conflicts-list", "usage": "tri conflicts list [--workspace <workspace>] [--status open|resolved]", "schema": "tri.conflicts.list", "description": "List persisted workspace conflicts."},
    {"name": "conflicts-inspect", "usage": "tri conflicts inspect <conflict>", "schema": "tri.conflicts.inspect", "description": "Inspect a persisted workspace conflict."},
    {"name": "conflicts-resolve", "usage": "tri conflicts resolve <conflict> --strategy ours|theirs|base", "schema": "tri.conflicts.resolve", "description": "Resolve a workspace conflict with an explicit strategy."},
    {"name": "files-list", "usage": "tri files list <workspace-or-snapshot>", "schema": "tri.files.list", "description": "List files in a workspace or snapshot."},
    {"name": "files-stat", "usage": "tri files stat <workspace-or-snapshot> <path>", "schema": "tri.files.stat", "description": "Inspect path metadata without reading file contents."},
    {"name": "files-read", "usage": "tri files read <workspace-or-snapshot> <path> [--offset <n>] [--limit <n>]", "schema": "tri.files.read", "description": "Read a file through the Tri file API."},
    {"name": "files-fetch", "usage": "tri files fetch <workspace-or-snapshot> --path <path> [--path <path>] [--max-bytes <n>]", "schema": "tri.files.fetch", "description": "Read several files in one call as base64 (remote mounts); paths beyond the byte budget come back in deferred."},
    {"name": "files-write", "usage": "tri files write <workspace> <path> (--text <text> | --base64 <data> | --body <file>) [--expect-head <snapshot>]", "schema": "tri.files.write", "description": "Write a file through the Tri file API."},
    {"name": "files-delete", "usage": "tri files delete <workspace> <path> [--expect-head <snapshot>]", "schema": "tri.files.delete", "description": "Delete a file through the Tri file API."},
    {"name": "files-move", "usage": "tri files move <workspace> <src> <dest> [--expect-head <snapshot>]", "schema": "tri.files.move", "description": "Move a file through the Tri file API."},
    {"name": "files-log", "usage": "tri files log <ref> <path> [--limit <n>]", "schema": "tri.files.log", "description": "Path history along the first-parent snapshot chain, with change provenance and no checkout."},
    {"name": "files-blame", "usage": "tri files blame <ref> <path>", "schema": "tri.files.blame", "description": "Attribute each line of a text file to the snapshot, actor, and changeset that introduced it, with no checkout."},
    {"name": "files-search", "usage": "tri files search <workspace-or-snapshot> <query> [--glob <glob>] [--limit <n>] [--case-sensitive]", "schema": "tri.files.search", "description": "Search visible text files through the Tri file API."},
    {"name": "operations-list", "usage": "tri operations list <workspace-or-changeset>", "schema": "tri.operation.list", "description": "List policy-readable workspace mutation operations."},
    {"name": "operations-inspect", "usage": "tri operations inspect <operation>", "schema": "tri.operation.inspect", "description": "Inspect one policy-gated operation provenance record."},
    {"name": "search", "usage": "tri search <project> <query> [--scope <scope>] [--glob <glob>] [--include-history] [--limit <n>] [--case-sensitive]", "schema": "tri.search", "description": "Search authorized project work with provenance across files and Tri objects."},
    {"name": "diff", "usage": "tri diff <workspace-or-changeset> | tri diff <base-snapshot> <head-snapshot> [--mode summary|files|patch|machine] [--path <glob>]", "schema": "tri.diff", "description": "Diff workspace, changeset, or explicit snapshots."},
    {"name": "run", "usage": "tri run <workspace> [--paths <glob>] [--sandbox] [--timeout <seconds>] [--with-lease <lease>] [--assumption <text>] [--no-import-changes] [--no-env] -- <command>", "schema": "tri.check.inspect", "description": "Run a command as a structured check run. --sandbox requires isolation (no network, writes only in the run directory, no Tri home or credential reads, scrubbed environment); --timeout kills the command's process group. --paths lays out only matching files (a partial checkout); changes anywhere are still imported, and only files that were laid out can be deleted."},
    {"name": "checks-list", "usage": "tri checks list <workspace-or-changeset>", "schema": "tri.check.list", "description": "List check runs for a workspace or changeset."},
    {"name": "checks-inspect", "usage": "tri checks inspect <check>", "schema": "tri.check.inspect", "description": "Inspect one structured check run."},
    {"name": "checks-set-create", "usage": "tri checks set-create <project> <name> --required-check <cmd>", "schema": "tri.check_set.inspect", "description": "Create a reusable project required-check set."},
    {"name": "merge-rules-add", "usage": "tri merge-rules add <project> <name> --path <glob> [--path <glob>] [--exclude <glob>] [--allow <selector>] [--max-files <n>] [--verify-after <check-set>]", "schema": "tri.merge_rule.inspect", "description": "Maintainer rule letting checked changesets that only touch matching paths merge without a human review. With --verify-after, matching changesets may merge before their checks run; the check set runs after merge and a failure reverts the change."},
    {"name": "merge-rules-list", "usage": "tri merge-rules list <project> [--all]", "schema": "tri.merge_rule.list", "description": "List project merge rules."},
    {"name": "merge-rules-inspect", "usage": "tri merge-rules inspect <project> <name-or-id>", "schema": "tri.merge_rule.inspect", "description": "Inspect one merge rule."},
    {"name": "mirror-add", "usage": "tri mirror add <project> --remote <url-or-path> [--target <target>] [--branch <branch>] [--audience <actor>] [--prefer git|tri] [--retry <n>] [--no-sync]", "schema": "tri.mirror.inspect", "description": "Maintainer two-way mirror between a Tri target and a Git branch; paths the audience cannot read and secret-like paths never leave Tri."},
    {"name": "mirror-list", "usage": "tri mirror list [<project>] [--all]", "schema": "tri.mirror.list", "description": "List Git mirrors."},
    {"name": "mirror-inspect", "usage": "tri mirror inspect <mirror-or-project>", "schema": "tri.mirror.inspect", "description": "Inspect one Git mirror and its last sync."},
    {"name": "mirror-sync", "usage": "tri mirror sync <mirror-or-project> [--retry <n>] [--watch [--interval <seconds>] [--max-cycles <n>]]", "schema": "tri.mirror.sync", "description": "Pull new Git commits into the target as snapshots, merge with Tri work, and push Tri merges back as Git commits."},
    {"name": "mirror-remove", "usage": "tri mirror remove <mirror-or-project>", "schema": "tri.mirror.inspect", "description": "Stop mirroring a target."},
    {"name": "verify-run", "usage": "tri verify run <project> [--limit <n>] [--watch [--interval <seconds>] [--max-cycles <n>]]", "schema": "tri.verification.run", "description": "Run queued post-merge verifications in merge order; a failure reverts the changeset on the current target."},
    {"name": "verify-list", "usage": "tri verify list <project> [--status pending|running|verified|reverted|revert_conflict]", "schema": "tri.verification.list", "description": "List post-merge verifications."},
    {"name": "verify-inspect", "usage": "tri verify inspect <verification>", "schema": "tri.verification.inspect", "description": "Inspect one post-merge verification."},
    {"name": "merge-rules-remove", "usage": "tri merge-rules remove <project> <name-or-id>", "schema": "tri.merge_rule.inspect", "description": "Remove a merge rule; later merges need an approved review again."},
    {"name": "checks-set-list", "usage": "tri checks set-list <project>", "schema": "tri.check_set.list", "description": "List reusable required-check sets for a project."},
    {"name": "checks-set-inspect", "usage": "tri checks set-inspect <project> <name-or-id> | <check-set>", "schema": "tri.check_set.inspect", "description": "Inspect a reusable required-check set."},
    {"name": "checks-artifacts", "usage": "tri checks artifacts <check>", "schema": "tri.check.artifacts", "description": "List artifacts produced by a check run."},
    {"name": "checks-stream", "usage": "tri checks stream <check> [--log stdout|stderr] [--offset <n>] [--limit <n>]", "schema": "tri.check.stream", "description": "Read a check log as an offset-addressed chunk."},
    {"name": "checks-rerun", "usage": "tri checks rerun <check>", "schema": "tri.check.inspect", "description": "Rerun a check command as a new check run."},
    {"name": "checks-compare", "usage": "tri checks compare <check...>", "schema": "tri.check.compare", "description": "Compare check-run outcomes."},
    {"name": "changeset-create", "usage": "tri changeset create <workspace> [--title <title>] [--visibility <selector>] [--hide-path <glob>] [--hide-package <pkg>]", "schema": "tri.changeset.inspect", "description": "Create a reviewable changeset from workspace operations."},
    {"name": "changeset-inspect", "usage": "tri changeset inspect <changeset>", "schema": "tri.changeset.inspect", "description": "Inspect a reviewable changeset and its provenance."},
    {"name": "changeset-split", "usage": "tri changeset split <changeset> (--paths <glob-list> | --operations <range-list>) [--apply] [--title <title>]", "schema": "tri.changeset.split", "description": "Preview or create a changeset split by path globs or operation ranges."},
    {"name": "changeset-stack", "usage": "tri changeset stack <changeset> --after <parent-changeset>", "schema": "tri.changeset.stack", "description": "Record a changeset dependency without creating branches."},
    {"name": "changeset-revalidate", "usage": "tri changeset revalidate <changeset> [--check <check>] [--required]", "schema": "tri.changeset.revalidate", "description": "Rerun checks against a changeset head snapshot without creating a branch."},
    {"name": "changeset-ready", "usage": "tri changeset ready <changeset> [--reason <text>]", "schema": "tri.changeset.lifecycle", "description": "Move a draft or changes-requested changeset back to ready for review."},
    {"name": "changeset-discard", "usage": "tri changeset discard <changeset> [--reason <text>]", "schema": "tri.changeset.lifecycle", "description": "Discard a draft or ready changeset while keeping it readable for audit."},
    {"name": "changeset-supersede", "usage": "tri changeset supersede <changeset> --by <replacement> [--reason <text>]", "schema": "tri.changeset.lifecycle", "description": "Mark a ready or reviewing changeset as superseded by a replacement changeset."},
    {"name": "changeset-merge", "usage": "tri changeset merge <changeset> --target <target> [--retry <n>]", "schema": "tri.changeset.merge", "description": "Policy-gated merge of an approved changeset into a Tri target."},
    {"name": "changeset-compare", "usage": "tri changeset compare <changeset...>", "schema": "tri.changeset.compare", "description": "Compare multiple agent attempts as changesets."},
    {"name": "review-create", "usage": "tri review create <changeset> [--reviewers <principal>]", "schema": "tri.review.create", "description": "Create a review object for a changeset."},
    {"name": "review-inspect", "usage": "tri review inspect <review>", "schema": "tri.review.inspect", "description": "Inspect review decision surface."},
    {"name": "review-action-inspect", "usage": "tri review action-inspect <review-action>", "schema": "tri.review_action.inspect", "description": "Inspect one durable review action."},
    {"name": "review-comment", "usage": "tri review comment <review> --body <text> [--path <path>] [--line <n>] [--reply-to <comment>]", "schema": "tri.review.action", "description": "Attach a structured review comment or reply."},
    {"name": "review-resolve-thread", "usage": "tri review resolve-thread <review> --thread <comment> [--body <text>]", "schema": "tri.review.action", "description": "Resolve a review comment thread."},
    {"name": "review-request-changes", "usage": "tri review request-changes <review> --body <text>", "schema": "tri.review.action", "description": "Request changes on a review."},
    {"name": "review-approve", "usage": "tri review approve <review> [--body <text>]", "schema": "tri.review.action", "description": "Approve a review and changeset."},
    {"name": "review-mark-risk", "usage": "tri review mark-risk <review> --risk <level> [--body <text>]", "schema": "tri.review.action", "description": "Record a structured risk marker on a review."},
    {"name": "review-attach-policy-finding", "usage": "tri review attach-policy-finding <review> [--body <text>]", "schema": "tri.review.action", "description": "Attach the current export policy decision to a review."},
    {"name": "review-attach-check-summary", "usage": "tri review attach-check-summary <review> [--body <text>]", "schema": "tri.review.action", "description": "Refresh and attach the current check summary to a review."},
    {"name": "review-close", "usage": "tri review close <review> [--body <text>]", "schema": "tri.review.action", "description": "Close a review without deleting its changeset."},
    {"name": "review-request-agent-pass", "usage": "tri review request-agent-pass <review> --task <title> [--assignee <actor>]", "schema": "tri.review.agent_pass", "description": "Create a follow-up agent task and workspace from a review."},
    {"name": "advisory-create", "usage": "tri advisory create <project> --title <title> [--body <text>] [--body-file <path>] [--affected-package <pkg>] [--trusted-reviewer <principal>] [--embargoed]", "schema": "tri.advisory.inspect", "description": "Create a private advisory coordination object."},
    {"name": "advisory-list", "usage": "tri advisory list <project>", "schema": "tri.advisory.list", "description": "List advisories for a project."},
    {"name": "advisory-inspect", "usage": "tri advisory inspect <advisory>", "schema": "tri.advisory.inspect", "description": "Inspect an advisory."},
    {"name": "release-prepare", "usage": "tri release prepare <project> --include <changeset> [--disclose <advisory>] [--package <pkg>] [--rollback-target <target>]", "schema": "tri.release.inspect", "description": "Prepare a release and optionally disclose advisories."},
    {"name": "release-list", "usage": "tri release list <project>", "schema": "tri.release.list", "description": "List prepared releases for a project."},
    {"name": "release-inspect", "usage": "tri release inspect <release>", "schema": "tri.release.inspect", "description": "Inspect a release record."},
    {"name": "policy-evaluate-export", "usage": "tri policy evaluate export <changeset> [--override-reason <reason>]", "schema": "tri.policy.decision", "description": "Evaluate export policy for a changeset."},
    {"name": "policy-evaluate-access", "usage": "tri policy evaluate access <object> [--action <action>]", "schema": "tri.policy.decision", "description": "Evaluate actor access to a Tri object."},
    {"name": "policy-explain", "usage": "tri policy explain <decision>", "schema": "tri.policy.explain", "description": "Explain a recorded policy decision by id."},
    {"name": "policy-create-embargo", "usage": "tri policy create-embargo <project> --title <title> [--body <text>] [--body-file <path>] [--affected-package <pkg>] [--trusted-reviewer <principal>]", "schema": "tri.policy.embargo", "description": "Create an embargoed advisory policy object."},
    {"name": "policy-disclose-embargo", "usage": "tri policy disclose-embargo <project> --advisory <advisory> --include <changeset> [--package <pkg>] [--rollback-target <target>]", "schema": "tri.policy.embargo_disclosure", "description": "Prepare a policy-gated release disclosure for embargoed work."},
    {"name": "policy-visibility", "usage": "tri policy visibility <object>", "schema": "tri.policy.visibility", "description": "Explain object visibility, selectors, and active grants."},
    {"name": "policy-grant", "usage": "tri policy grant <object> --principal actor:<id>|role:<name> [--action <action>]", "schema": "tri.policy.grant", "description": "Grant an actor or role access to a Tri object."},
    {"name": "policy-grant-inspect", "usage": "tri policy grant-inspect <grant>", "schema": "tri.policy.grant", "description": "Inspect a local object policy grant."},
    {"name": "policy-revoke", "usage": "tri policy revoke <grant>", "schema": "tri.policy.grant", "description": "Revoke a local object policy grant."},
    {"name": "config-materialize", "usage": "tri config materialize <workspace> --profile <profile>", "schema": "tri.config.materialize", "description": "Create a non-exportable generated config artifact."},
    {"name": "secrets-put", "usage": "tri secrets put <project> <name> --value-file <path> [--scope <scope>]", "schema": "tri.secrets.inspect", "description": "Store a local secret reference outside source snapshots."},
    {"name": "secrets-list", "usage": "tri secrets list <project>", "schema": "tri.secrets.list", "description": "List local secret references without exposing values."},
    {"name": "secrets-inspect", "usage": "tri secrets inspect <secret> [--project <project>]", "schema": "tri.secrets.inspect", "description": "Inspect policy-gated secret metadata without exposing the value."},
    {"name": "secrets-request-lease", "usage": "tri secrets request-lease <workspace> <secret> --env <name> [--ttl-seconds <n>]", "schema": "tri.secret_lease.inspect", "description": "Grant a scoped secret lease to a workspace."},
    {"name": "secrets-revoke", "usage": "tri secrets revoke <lease>", "schema": "tri.secret_lease.inspect", "description": "Revoke a secret lease."},
    {"name": "secrets-audit", "usage": "tri secrets audit [--workspace <workspace>] [--project <project>] [--secret <secret>] [--limit <n>]", "schema": "tri.secrets.audit", "description": "List secret and lease audit events."},
    {"name": "secrets-redact-logs", "usage": "tri secrets redact-logs <check> [--lease <lease>]", "schema": "tri.secrets.redact_logs", "description": "Reprocess check log artifacts with known secret lease redactions."},
    {"name": "events-list", "usage": "tri events list [--workspace <workspace>] [--project <project>] [--kind <kind>] [--limit <n>]", "schema": "tri.events.list", "description": "List append-only provenance events."},
    {"name": "events-inspect", "usage": "tri events inspect <event>", "schema": "tri.events.inspect", "description": "Inspect one provenance event."},
    {"name": "artifacts-list", "usage": "tri artifacts list [--kind <kind>] [--label <label>] [--limit <n>]", "schema": "tri.artifacts.list", "description": "List artifacts the actor can read in the local store."},
    {"name": "artifacts-inspect", "usage": "tri artifacts inspect <artifact>", "schema": "tri.artifacts.inspect", "description": "Inspect a policy-gated artifact record."},
    {"name": "artifacts-read", "usage": "tri artifacts read <artifact>", "schema": "tri.artifacts.read", "description": "Read policy-gated artifact content for logs and generated files."},
    {"name": "export-git-patch", "usage": "tri export git-patch <changeset> [--output <path>] [--override-reason <reason>] [--summary]", "schema": "tri.export.git_patch", "description": "Policy-gated export of a changeset as a Git patch."},
    {"name": "export-git-commit", "usage": "tri export git-commit <changeset> [--repo <repo>] [--message <text>] [--override-reason <reason>]", "schema": "tri.export.git_commit", "description": "Policy-gated export of a changeset as a Git commit in a clean repo."},
    {"name": "export-github-pr", "usage": "tri export github-pr <changeset> --repo <repo> --branch <branch> [--base <base>] [--title <title>] [--body <text>] [--draft] [--create] [--override-reason <reason>]", "schema": "tri.export.github_pr", "description": "Prepare a policy-gated GitHub PR branch and request artifact."},
    {"name": "export-list", "usage": "tri export list <project> [--changeset <changeset>] [--kind <kind>] [--limit <n>]", "schema": "tri.export.list", "description": "List policy-visible export records for a project."},
    {"name": "export-inspect", "usage": "tri export inspect <export>", "schema": "tri.export.inspect", "description": "Inspect an export record with policy decision and source event range."},
]


SCHEMA_CATALOG: dict[str, dict[str, Any]] = {
    "tri.response": {
        "required": ["ok", "schema", "schema_version", "command", "actor", "data", "events", "diagnostics", "warnings", "cost", "refs", "links"],
        "properties": {
            "ok": "bool",
            "schema": "string",
            "schema_version": "semver",
            "command": "list<string>",
            "actor": "actor id",
            "data": "schema-specific object",
            "events": "list<evt_ id>",
            "diagnostics": "list<Diagnostic>",
            "warnings": "list<Diagnostic>",
            "cost": "Cost",
            "refs": "object",
            "links": "object",
        },
    },
    "tri.api.health": {"required": ["status", "home", "schema_version"]},
    "tri.api.command": {"required": ["argv"]},
    "tri.api.batch": {"required": ["count", "executed", "ok_count", "failed", "results"]},
    "tri.api.server": {"required": ["host", "port", "endpoints"]},
    "tri.diagnostic": {
        "required": ["code", "severity", "message", "retryable"],
        "codes": [
            "remote_unreachable",
            "content_mismatch",
            "sandbox_unavailable",
            "mirror_conflict",
            "mirror_diverged",
            "mirror_push_rejected",
            "mirror_history_rewritten",
            "git_mirror_failed",
            "git_mirror_unreachable",
            "stale_workspace_head",
            "stale_materialization",
            "lease_already_reaped",
            "mount_path_in_use",
            "materialization_degraded",
            "content_purged",
            "gc_in_progress",
            "salvage_already_adopted",
            "env_build_failed",
            "env_output_missing",
            "policy_denied",
            "path_not_found",
            "path_escaped_mount",
            "invalid_path",
            "secret_export_blocked",
            "non_exportable_artifact",
            "conflict_required",
            "unknown_actor",
            "missing_required_check",
            "missing_review_approval",
            "git_export_failed",
            "visibility_export_blocked",
            "stale_target_snapshot",
            "unsatisfied_changeset_dependency",
            "advisory_embargo_active",
            "reviewer_not_assigned",
            "review_closed",
            "task_lifecycle_blocked",
            "workspace_lifecycle_blocked",
            "changeset_lifecycle_blocked",
            "redacted_changeset",
            "unresolved_conflict",
            "invalid_command",
            "unauthenticated",
            "actor_mismatch",
        ],
    },
    "tri.commands": {"required": ["schema_version", "commands"]},
    "tri.schema": {"required_any": [["schema_version", "schemas"], ["schema_version", "name", "schema"]]},
    "tri.batch": {"required": ["count", "executed", "ok_count", "failed", "results"]},
    "tri.doctor": {"required": ["home", "db", "schema_version", "counts", "git_available", "command_count"]},
    "tri.object.inspect": {"required": ["object", "object_type", "resolved_command", "resolved_schema", "result_ok", "result"]},
    "tri.actor.inspect": {"required": ["id", "kind", "known", "task_scope", "principal", "actor_selector", "selectors", "roles", "capabilities", "accepted_formats"]},
    "tri.import": {"required": ["project", "target", "snapshot", "source", "files"]},
    "tri.project.inspect": {"required": ["id", "name", "root_path", "default_target", "targets", "package_boundaries", "redacted_package_boundaries", "visibility_defaults", "policy_set", "import_settings", "export_settings", "counts", "created_at"]},
    "tri.project.list": {"required": ["projects"]},
    "tri.package.inspect": {"required": ["id", "project", "name", "path_globs", "visibility", "owners", "created_by", "created_at", "access"]},
    "tri.package.list": {"required": ["project", "packages", "redacted"]},
    "tri.targets.list": {"required": ["project", "targets"]},
    "tri.targets.inspect": {"required": ["id", "project", "name", "snapshot", "snapshot_data", "created_at"]},
    "tri.targets.refresh": {"required": ["target", "name", "snapshot", "source"]},
    "tri.snapshot.inspect": {"required": ["id", "project", "root_tree", "parents", "source", "content_fingerprint", "created_by", "created_at", "metadata", "file_count", "byte_count", "targets", "workspaces", "changesets", "checks", "releases", "source_events", "redacted", "access"]},
    "tri.snapshot.list": {"required": ["project", "snapshots", "redacted"]},
    "tri.tree.inspect": {"required": ["id", "hash", "fingerprint", "entries", "file_count", "byte_count", "redacted_entries", "origins", "origin_count", "redacted_origins", "access"]},
    "tri.blob.inspect": {"required": ["id", "hash", "fingerprint", "size", "binary", "origins", "origin_count", "redacted_origins", "access"]},
    "tri.workspace.list": {"required": ["project", "workspaces", "redacted"]},
    "tri.workspace.create": {"required": ["id", "project", "base_snapshot", "head_snapshot", "task", "task_run", "status", "visibility", "hidden_paths", "advisory"]},
    "tri.workspace.inspect": {"required": ["id", "project", "base_snapshot", "head_snapshot", "owner", "task", "task_run", "status", "visibility", "hidden_paths", "advisory", "last_operation", "materializations", "checks", "redacted_checks", "changesets", "redacted_changesets"]},
    "tri.workspace.update": {"required": ["workspace", "base_snapshot", "head_snapshot", "conflicts"]},
    "tri.workspace.snapshot": {"required": ["workspace", "operation", "snapshot"]},
    "tri.ignore.list": {"required": ["project", "patterns"]},
    "tri.gc": {"required": ["applied", "plan", "reaped", "evicted", "purged"]},
    "tri.env.recipe": {"required": ["id", "project", "name", "key_files", "key_commands", "install", "outputs", "mode"]},
    "tri.env.list": {"required": ["project", "recipes"]},
    "tri.env.inspect": {"required": ["recipe", "builds"]},
    "tri.lease.list": {"required": ["workspace", "leases"]},
    "tri.lease.reap": {"required": ["reaped", "salvaged", "needs_attention", "errors", "checked", "busy", "swept"]},
    "tri.salvage.list": {"required": ["workspace", "salvages"]},
    "tri.salvage.adopt": {"required": ["salvage", "workspace", "operation", "snapshot", "paths"]},
    "tri.disk_lease.inspect": {"required": ["id", "kind", "path", "workspace", "status", "backend", "created_at", "released_at"]},
    "tri.workspace.sync": {"required": ["workspace", "lease", "operation", "snapshot", "paths"]},
    "tri.workspace.mount": {"required": ["workspace", "path", "snapshot", "lease", "read_only", "materialization"]},
    "tri.workspace.materialization_lease": {"required": ["id", "workspace", "path", "status", "read_only", "mounted_snapshot", "last_sync_snapshot", "owner", "ignored_paths", "generated_artifacts", "expires_at", "created_at", "revoked_at"]},
    "tri.workspace.materialization_leases": {"required": ["workspace", "leases"]},
    "tri.lease.inspect": {"required": ["id", "kind", "schema", "lease"]},
    "tri.task.inspect": {"required": ["id", "project", "title", "body", "requester", "status", "acceptance_criteria", "required_checks", "lifecycle", "workspaces", "runs", "changesets", "redacted", "created_at"]},
    "tri.task_run.inspect": {"required": ["id", "task", "actor", "workspace", "redacted_workspace", "status", "final_recommendation", "assumptions", "commands", "checks", "artifacts", "changesets", "cost", "redacted", "created_at"]},
    "tri.task.lifecycle": {"required": ["id", "status", "previous_status", "transition", "lifecycle"]},
    "tri.workspace.lifecycle": {"required": ["id", "status", "previous_status", "transition"]},
    "tri.conflicts.list": {"required": ["conflicts", "redacted"]},
    "tri.conflicts.inspect": {"required": ["id", "workspace", "status", "conflicts"]},
    "tri.conflicts.resolve": {"required": ["conflict", "workspace", "operation", "strategy", "snapshot"]},
    "tri.files.stat": {"required": ["snapshot", "path", "exists", "type", "visible"]},
    "tri.files.list": {"required": ["snapshot", "files", "redacted_skipped", "policy_denied_skipped"]},
    "tri.files.read": {"required": ["snapshot", "path", "blob", "encoding", "text"]},
    "tri.files.write": {"required": ["operation", "snapshot", "paths"]},
    "tri.files.delete": {"required": ["operation", "snapshot", "paths"]},
    "tri.files.move": {"required": ["operation", "snapshot", "paths"]},
    "tri.files.search": {"required": ["snapshot", "query", "matches", "redacted_skipped", "truncated"]},
    "tri.operation.inspect": {"required": ["id", "workspace", "workspace_redacted", "actor", "kind", "input_snapshot", "output_snapshot", "paths", "redacted_paths", "policy", "metadata", "changesets", "redacted_changesets", "source_events", "redacted_source_events", "access", "created_at"]},
    "tri.operation.list": {"required": ["ref", "operations", "redacted"]},
    "tri.search": {"required": ["project", "query", "scopes", "matches", "policy_denied_skipped", "redacted_skipped", "truncated"]},
    "tri.diff": {"required": ["base_snapshot", "head_snapshot", "mode", "path_filters", "summary", "files", "patch"]},
    "tri.changeset.inspect": {"required": ["id", "project", "workspace", "redacted_workspace", "title", "status", "base_snapshot", "head_snapshot", "operations", "operation_details", "task_run", "visibility", "hidden_paths", "touched_paths", "redacted_touched_paths", "checks", "reviews", "policy_decisions", "export_status", "advisory", "replacement_changeset", "lifecycle", "suggested_split_points", "stack"]},
    "tri.changeset.split": {"required": ["changeset", "mode", "selector", "operations", "paths", "redacted_paths"]},
    "tri.changeset.stack": {"required": ["changeset", "stack"]},
    "tri.changeset.revalidate": {"required": ["changeset", "head_snapshot", "checks", "stack"]},
    "tri.changeset.lifecycle": {"required": ["id", "status", "previous_status", "transition", "lifecycle"]},
    "tri.changeset.merge": {"required": ["changeset", "target", "status", "previous_snapshot", "snapshot", "changed_paths", "conflicts"]},
    "tri.changeset.compare": {"required": ["changesets", "redacted"]},
    "tri.check.list": {"required": ["ref", "object_type", "workspace", "redacted_workspace", "changeset", "checks", "redacted"]},
    "tri.check.inspect": {"required": ["id", "workspace", "changeset", "redacted_workspace", "redacted_changeset", "linked_changesets", "access", "task_run", "rerun_of", "actor", "status", "runner", "command", "snapshot_before", "snapshot_after", "exit_status", "duration_ms", "logs", "artifacts", "secret_leases", "materialized_directory", "environment_policy", "inputs", "outputs", "file_changes", "redacted_file_changes", "cost", "assumptions", "reproducibility", "created_at"]},
    "tri.check.artifacts": {"required": ["check", "artifacts"]},
    "tri.check.stream": {"required": ["check", "log", "artifact", "offset", "next_offset", "limit", "total_bytes", "eof", "encoding", "text"]},
    "tri.check.compare": {"required": ["checks"]},
    "tri.check_set.inspect": {"required": ["id", "project", "name", "required_checks", "created_by", "created_at"]},
    "tri.check_set.list": {"required": ["project", "check_sets"]},
    "tri.auth.token": {"required": ["id", "actor", "token", "file", "shown_once"]},
    "tri.auth.list": {"required": ["file", "tokens"]},
    "tri.mcp.server": {"required": ["protocol_version", "tools", "actor"]},
    "tri.files.log": {"required": ["ref", "snapshot", "path", "entries", "walked", "truncated"]},
    "tri.files.blame": {"required": ["ref", "snapshot", "path", "line_count", "ranges", "boundary", "walked", "truncated"]},
    "tri.snapshot.merge_base": {"required": ["left", "right", "merge_base", "redacted"]},
    "tri.merge_rule.inspect": {"required": ["id", "project", "name", "path_globs", "exclude_globs", "allow", "max_files", "status", "created_by", "created_at"]},
    "tri.merge_rule.list": {"required": ["project", "merge_rules"]},
    "tri.mirror.inspect": {"required": ["id", "project", "target", "remote", "branch", "audience", "status", "synced_snapshot", "synced_git_commit", "in_sync", "last_result", "created_by", "created_at"]},
    "tri.mirror.list": {"required": ["mirrors"]},
    "tri.files.fetch": {"required": ["snapshot", "files", "missing", "deferred", "bytes"]},
    "tri.remote.mount": {"required": ["workspace", "path", "snapshot", "remote", "files", "fetched_bytes", "cache_hits"]},
    "tri.remote.sync": {"required": ["workspace", "path", "snapshot", "remote", "written", "deleted"]},
    "tri.verification.inspect": {"required": ["id", "project", "changeset", "target", "merged_snapshot", "pre_merge_snapshot", "check_set", "status", "checks", "created_at"]},
    "tri.verification.list": {"required": ["project", "verifications"]},
    "tri.verification.run": {"required": ["project", "processed", "pending", "cycles"]},
    "tri.mirror.sync": {"required": ["id", "project", "target", "remote", "branch", "synced_snapshot", "synced_git_commit", "in_sync", "sync", "cycles"]},
    "tri.review.create": {"required": ["id", "changeset", "status", "reviewers", "check_summary", "export_preview"]},
    "tri.review.inspect": {"required": ["id", "changeset", "redacted_changeset", "workspace", "redacted_workspace", "status", "reviewers", "approvals", "approved_reviewers", "pending_reviewers", "maintainer_override", "check_summary", "risk_summary", "export_preview", "changeset_summary", "task", "task_run", "actor_identity", "commands", "checks", "artifacts", "policy_findings", "attached_policy_findings", "attached_check_summaries", "visibility_impact", "comment_threads", "unresolved_risks", "actions", "created_at"]},
    "tri.review_action.inspect": {"required": ["id", "review", "actor", "kind", "body", "metadata", "created_at"]},
    "tri.review.action": {"required": ["review", "action", "status"]},
    "tri.review.agent_pass": {"required": ["review", "action", "task", "workspace", "task_run"]},
    "tri.advisory.inspect": {"required": ["id", "project", "title", "body", "status", "disclosure_state", "embargoed", "affected_packages", "trusted_reviewers", "private_changesets", "redacted", "created_by", "created_at", "disclosed_at"]},
    "tri.advisory.list": {"required": ["project", "advisories", "redacted"]},
    "tri.release.inspect": {"required": ["id", "project", "status", "target_snapshot", "target_snapshot_redacted", "included_changesets", "packages", "artifacts", "checks", "advisories_disclosed", "redacted", "rollback_target", "policy_decisions", "created_by", "created_at"]},
    "tri.release.list": {"required": ["project", "releases", "redacted"]},
    "tri.secrets.inspect": {"required": ["id", "project", "name", "provider", "fingerprint", "scope", "created_by", "created_at", "revoked_at"]},
    "tri.secrets.list": {"required": ["project", "secrets", "redacted"]},
    "tri.secret_lease.inspect": {"required": ["id", "secret", "workspace", "actor", "env", "status", "expires_at", "created_at", "revoked_at"]},
    "tri.secrets.audit": {"required": ["events", "redacted"]},
    "tri.secrets.redact_logs": {"required": ["check", "leases", "artifacts", "redacted_artifacts", "replacement_count"]},
    "tri.config.materialize": {"required": ["workspace", "artifact", "labels"]},
    "tri.artifacts.list": {"required": ["artifacts", "redacted"]},
    "tri.artifacts.inspect": {"required": ["id", "kind", "path", "storage", "size", "fingerprint", "labels", "created_at", "origins", "access"]},
    "tri.artifacts.read": {"required": ["id", "kind", "path", "storage", "size", "fingerprint", "labels", "created_at", "origins", "access", "encoding", "text"]},
    "tri.export.inspect": {"required": ["id", "status", "project", "changeset", "workspace", "redacted_workspace", "export_kind", "target_remote", "target_ref", "exported_object_ids", "policy_decision", "actor", "artifact", "diagnostics", "source_events", "determinism", "created_at", "completed_at"]},
    "tri.export.list": {"required": ["project", "exports", "redacted"]},
    "tri.export.git_patch": {"required": ["export", "record"]},
    "tri.export.git_commit": {"required": ["export", "record"]},
    "tri.export.github_pr": {"required": ["export", "record"]},
    "tri.policy.decision": {
        "required": ["id", "allowed", "actor", "action", "object", "policies", "grants", "denials", "redactions", "reason_code", "event"],
        "required_any": [
            ["object_type", "visibility", "owner", "actor_selectors", "matched_grants", "reason"],
            ["checks", "review", "stack", "changeset_lifecycle", "object_access", "override"],
        ],
    },
    "tri.policy.explain": {"required": ["id", "decision", "event", "action", "object", "recorded_at"]},
    "tri.policy.embargo": {"required": ["id", "project", "title", "body", "status", "disclosure_state", "embargoed", "affected_packages", "trusted_reviewers", "private_changesets", "redacted", "created_by", "created_at", "disclosed_at"]},
    "tri.policy.embargo_disclosure": {"required": ["id", "project", "status", "target_snapshot", "target_snapshot_redacted", "included_changesets", "packages", "artifacts", "checks", "advisories_disclosed", "redacted", "rollback_target", "policy_decisions", "created_by", "created_at"]},
    "tri.policy.visibility": {"required": ["object", "object_type", "project", "visibility", "owner", "actor_selectors", "direct_grants", "inherited_grants"]},
    "tri.policy.grant": {"required": ["id", "object", "principal", "actions", "status"]},
    "tri.task.attempts": {"required": ["task", "workspaces", "runs", "changesets", "redacted"]},
    "tri.events.list": {"required": ["events", "redacted"]},
    "tri.events.inspect": {"required": ["id", "actor", "project", "workspace", "redacted_workspace", "task", "kind", "object_refs", "input_fingerprints", "output_fingerprints", "policy_result", "created_at"]},
}


class TriError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        severity: str = "error",
        retryable: bool = False,
        **fields: Any,
    ) -> None:
        super().__init__(message)
        self.diagnostic = {
            "code": code,
            "severity": severity,
            "message": message,
            "retryable": retryable,
            **fields,
        }


class TriArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TriError("invalid_command", message)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def response_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def compact_tree_entries(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    compact: dict[str, dict[str, Any]] = {}
    for path, entry in entries.items():
        compact_entry = dict(entry)
        compact_entry.pop("hash", None)
        compact[path] = compact_entry
    return compact


def encode_tree_json(entries: dict[str, dict[str, Any]]) -> tuple[bytes, str]:
    raw = dumps(entries).encode()
    stored_raw = dumps(compact_tree_entries(entries)).encode()
    compressed = base64.b64encode(zlib.compress(stored_raw)).decode()
    encoded = f"{COMPRESSED_TREE_JSON_PREFIX}{compressed}"
    if len(encoded.encode()) < len(stored_raw):
        return raw, encoded
    return raw, stored_raw.decode()


def decode_tree_json(value: str) -> dict[str, dict[str, Any]]:
    if value.startswith(COMPRESSED_TREE_JSON_PREFIX):
        raw = zlib.decompress(base64.b64decode(value.removeprefix(COMPRESSED_TREE_JSON_PREFIX)))
        return json.loads(raw.decode())
    return loads(value, {})


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise TriError("invalid_path", "Stored paths must be relative.", path=path)
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise TriError("invalid_path", "Stored paths cannot be empty or contain . or ...", path=path)
    return "/".join(parts)


def is_secret_like_path(path: str) -> bool:
    name = PurePosixPath(path).name
    patterns = [
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_ed25519",
        "*credentials*",
        "*secret*",
    ]
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def normalize_hidden_patterns(patterns: list[str] | None) -> list[str]:
    if not patterns:
        return []
    normalized = []
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        normalized.append(normalize_path(pattern))
    return sorted(set(normalized))


def normalize_path_patterns(patterns: list[str] | None) -> list[str]:
    return normalize_hidden_patterns(patterns)


def path_matches_hidden(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or path.startswith(f"{pattern}/") for pattern in patterns)


def path_matches_filters(path: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(path, pattern) or path.startswith(f"{pattern}/") for pattern in patterns)


def path_visibility(path: str, hidden_patterns: list[str] | None = None) -> tuple[bool, str | None]:
    if is_secret_like_path(path):
        return False, "secret_like_path"
    if path_matches_hidden(path, hidden_patterns or []):
        return False, "hidden_path"
    return True, None


def redacted_path_ref(path: str) -> dict[str, Any]:
    return {"path": None, "path_hash": f"sha256:{sha256(path.encode())[:16]}"}


def decision_denial(code: str, message: str, **fields: Any) -> dict[str, Any]:
    return TriError(code, message, **fields).diagnostic


class Store:
    def __init__(self, home: Path | None = None, *, pack_on_close: bool | None = None) -> None:
        self.home = Path(os.environ.get("TRI_HOME", home or ".tri")).resolve()
        self.objects = self.home / "objects"
        self.blobs = self.objects / "blobs"
        self.artifacts = self.home / "artifacts"
        self.runs = self.home / "runs"
        self.secrets = self.home / "secrets"
        self.checkout = self.home / "checkout"
        self.leases_dir = self.home / "leases"
        self._materializer: Materializer | None = None
        self.db_path = self.home / "tri.sqlite3"
        self.packed_db_path = self.home / "tri.sqlite3.tri"
        self.lock_path = self.home / "tri.sqlite3.lock"
        self.lock_file: Any | None = None
        self._blob_cache: dict[str, bytes] = {}
        self._tree_cache: dict[str, tuple[dict[str, dict[str, Any]], set[str]]] = {}
        self.home.mkdir(parents=True, exist_ok=True)
        # Two locks, so no lock is ever converted between shared and exclusive
        # (flock conversion is not atomic):
        # - the open lock is held shared by every open Store; packing takes it
        #   exclusive, so the database is only removed when nobody has it open;
        # - the admin lock serializes unpacking, schema setup, and packing.
        # Opening therefore never waits on another open Store, only on a pack or
        # unpack in progress.
        self.lock_file = FileLock(self.lock_path)
        self.lock_file.acquire(exclusive=False)
        self.admin_lock = FileLock(self.home / "tri.sqlite3.admin.lock")
        self.admin_lock.acquire(exclusive=True)
        try:
            self._open_db(pack_on_close)
        finally:
            self.admin_lock.release()

    def _open_db(self, pack_on_close: bool | None) -> None:
        self.pack_db_max_bytes: int | None = None
        if pack_on_close is None:
            pack_value = os.environ.get("TRI_SQLITE_PACK", "auto").lower()
            if pack_value == "auto":
                pack_on_close = True
                try:
                    self.pack_db_max_bytes = int(os.environ.get("TRI_SQLITE_PACK_MAX_BYTES", "100000"))
                except ValueError:
                    self.pack_db_max_bytes = 100000
            else:
                pack_on_close = pack_value not in {"0", "false", "no", "off"}
        self.pack_on_close = pack_on_close
        if not self.db_path.exists() and self.packed_db_path.exists():
            self.unpack_db()
        new_db = not self.db_path.exists()
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if new_db:
            try:
                page_size = int(os.environ.get("TRI_SQLITE_PAGE_SIZE", "512"))
            except ValueError:
                page_size = 512
            if page_size not in {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}:
                page_size = 512
            self.conn.execute(f"PRAGMA page_size={page_size}")
        self.init_db()

    @property
    def materializer(self) -> Materializer:
        if self._materializer is None:
            self._materializer = Materializer(self.home, CheckoutCache(self.checkout, self.read_blob), read_blob=self.read_blob, load_tree=self.load_tree)
        return self._materializer

    def init_db(self) -> None:
        journal_mode = os.environ.get("TRI_SQLITE_JOURNAL", "DELETE").upper()
        if journal_mode not in {"DELETE", "WAL", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}:
            journal_mode = "DELETE"
        self.conn.execute(f"PRAGMA journal_mode={journal_mode}")
        self.conn.execute("PRAGMA journal_size_limit=0")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY,
              name TEXT UNIQUE NOT NULL,
              root_path TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS targets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              name TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS blobs (
              id TEXT PRIMARY KEY,
              hash TEXT UNIQUE NOT NULL,
              size INTEGER NOT NULL,
              object_path TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trees (
              id TEXT PRIMARY KEY,
              hash TEXT UNIQUE NOT NULL,
              entries_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              root_tree_id TEXT NOT NULL,
              parent_ids_json TEXT NOT NULL,
              source TEXT NOT NULL,
              content_fingerprint TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspaces (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              base_snapshot_id TEXT NOT NULL,
              head_snapshot_id TEXT NOT NULL,
              owner_actor TEXT NOT NULL,
              task_id TEXT,
              status TEXT NOT NULL,
              visibility_json TEXT NOT NULL,
              hidden_paths_json TEXT NOT NULL DEFAULT '[]',
              advisory_id TEXT,
              materialized_path TEXT,
              materialized_snapshot_id TEXT,
              ttl TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              kind TEXT NOT NULL,
              input_snapshot_id TEXT NOT NULL,
              output_snapshot_id TEXT NOT NULL,
              path_set_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              actor TEXT NOT NULL,
              project_id TEXT,
              workspace_id TEXT,
              task_id TEXT,
              kind TEXT NOT NULL,
              object_refs_json TEXT NOT NULL,
              input_fingerprints_json TEXT NOT NULL,
              output_fingerprints_json TEXT NOT NULL,
              policy_result_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '',
              requester TEXT NOT NULL,
              acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
              required_checks_json TEXT NOT NULL DEFAULT '[]',
              lifecycle_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_runs (
              id TEXT PRIMARY KEY,
              task_id TEXT,
              actor TEXT NOT NULL,
              workspace_id TEXT,
              status TEXT NOT NULL,
              final_recommendation TEXT,
              assumptions_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS check_runs (
              id TEXT PRIMARY KEY,
              workspace_id TEXT,
              changeset_id TEXT,
              task_run_id TEXT,
              rerun_of TEXT,
              actor TEXT NOT NULL DEFAULT 'actor_local',
              status TEXT NOT NULL,
              runner TEXT NOT NULL,
              command_json TEXT NOT NULL,
              snapshot_before TEXT NOT NULL,
              snapshot_after TEXT NOT NULL,
              exit_status INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL,
              logs_json TEXT NOT NULL,
              artifacts_json TEXT NOT NULL,
              secret_leases_json TEXT NOT NULL,
              materialized_directory_json TEXT NOT NULL DEFAULT '{}',
              environment_policy_json TEXT NOT NULL DEFAULT '{}',
              file_changes_json TEXT NOT NULL,
              cost_json TEXT NOT NULL DEFAULT '{}',
              assumptions_json TEXT NOT NULL DEFAULT '[]',
              reproducibility TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS check_sets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              name TEXT NOT NULL,
              required_checks_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS package_boundaries (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              name TEXT NOT NULL,
              path_globs_json TEXT NOT NULL,
              visibility_json TEXT NOT NULL,
              owners_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS changesets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              workspace_id TEXT,
              title TEXT NOT NULL,
              base_snapshot_id TEXT NOT NULL,
              head_snapshot_id TEXT NOT NULL,
              status TEXT NOT NULL,
              operations_json TEXT NOT NULL,
              task_run_id TEXT,
              visibility_json TEXT NOT NULL,
              hidden_paths_json TEXT NOT NULL DEFAULT '[]',
              touched_paths_json TEXT NOT NULL,
              checks_json TEXT NOT NULL,
              reviews_json TEXT NOT NULL,
              policy_decisions_json TEXT NOT NULL,
              export_status TEXT NOT NULL,
              stack_parent_id TEXT,
              advisory_id TEXT,
              replacement_changeset_id TEXT,
              lifecycle_json TEXT NOT NULL DEFAULT '{}',
              suggested_split_points_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
              id TEXT PRIMARY KEY,
              changeset_id TEXT NOT NULL,
              status TEXT NOT NULL,
              reviewers_json TEXT NOT NULL,
              check_summary_json TEXT NOT NULL,
              risk_summary TEXT NOT NULL,
              export_preview_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_actions (
              id TEXT PRIMARY KEY,
              review_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              kind TEXT NOT NULL,
              body TEXT,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              size INTEGER NOT NULL,
              labels_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exports (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              changeset_id TEXT NOT NULL,
              export_kind TEXT NOT NULL,
              target_remote TEXT,
              target_ref TEXT,
              exported_object_ids_json TEXT NOT NULL,
              policy_decision_json TEXT NOT NULL,
              actor TEXT NOT NULL,
              artifact_id TEXT,
              diagnostics_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS workspace_conflicts (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              base_snapshot_id TEXT NOT NULL,
              head_snapshot_id TEXT NOT NULL,
              target_snapshot_id TEXT NOT NULL,
              target_ref TEXT NOT NULL,
              conflicts_json TEXT NOT NULL,
              status TEXT NOT NULL,
              resolution_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS secret_refs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              name TEXT NOT NULL,
              provider TEXT NOT NULL,
              value_path TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              scope_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              revoked_at TEXT,
              UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS secret_leases (
              id TEXT PRIMARY KEY,
              secret_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              env_name TEXT NOT NULL,
              status TEXT NOT NULL,
              expires_at TEXT,
              created_at TEXT NOT NULL,
              revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS materialization_leases (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              path TEXT NOT NULL,
              mounted_snapshot_id TEXT NOT NULL,
              last_sync_snapshot_id TEXT NOT NULL,
              ignored_paths_json TEXT NOT NULL,
              generated_artifacts_json TEXT NOT NULL,
              status TEXT NOT NULL,
              expires_at TEXT,
              created_at TEXT NOT NULL,
              revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS disk_leases (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              path TEXT NOT NULL,
              workspace_id TEXT,
              check_id TEXT,
              before_snapshot_id TEXT,
              baseline_path TEXT,
              holder_lock TEXT,
              backend TEXT,
              status TEXT NOT NULL,
              salvage_id TEXT,
              created_at TEXT NOT NULL,
              released_at TEXT
            );
            CREATE TABLE IF NOT EXISTS salvages (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              lease_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              base_snapshot_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              paths_json TEXT NOT NULL,
              deleted_ignored_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              adopted_at TEXT,
              adopted_by TEXT
            );
            CREATE TABLE IF NOT EXISTS store_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS env_recipes (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              name TEXT NOT NULL,
              key_files_json TEXT NOT NULL,
              key_commands_json TEXT NOT NULL,
              install TEXT NOT NULL,
              outputs_json TEXT NOT NULL,
              mode TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE (project_id, name)
            );
            CREATE TABLE IF NOT EXISTS env_builds (
              key TEXT PRIMARY KEY,
              recipe_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              status TEXT NOT NULL,
              path TEXT NOT NULL,
              outputs_json TEXT NOT NULL,
              size_bytes INTEGER,
              exit_status INTEGER,
              stdout_artifact TEXT,
              stderr_artifact TEXT,
              error_json TEXT,
              workspace_id TEXT,
              built_by TEXT,
              created_at TEXT NOT NULL,
              last_used_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_ignores (
              project_id TEXT NOT NULL,
              pattern TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (project_id, pattern)
            );
            CREATE TABLE IF NOT EXISTS policy_grants (
              id TEXT PRIMARY KEY,
              project_id TEXT,
              object_type TEXT NOT NULL,
              object_id TEXT NOT NULL,
              principal TEXT NOT NULL,
              actions_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS verifications (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              changeset_id TEXT NOT NULL,
              target_id TEXT NOT NULL,
              merged_snapshot_id TEXT NOT NULL,
              pre_merge_snapshot_id TEXT NOT NULL,
              check_set_id TEXT NOT NULL,
              merge_rule_id TEXT,
              status TEXT NOT NULL,
              checks_json TEXT NOT NULL DEFAULT '[]',
              revert_snapshot_id TEXT,
              result_json TEXT NOT NULL DEFAULT '{}',
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS mirrors (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              target_id TEXT NOT NULL,
              remote TEXT NOT NULL,
              branch TEXT NOT NULL,
              audience TEXT NOT NULL,
              prefer TEXT,
              status TEXT NOT NULL,
              synced_snapshot_id TEXT,
              synced_git_commit TEXT,
              event_cursor INTEGER NOT NULL DEFAULT 0,
              last_sync_at TEXT,
              last_result_json TEXT NOT NULL DEFAULT '{}',
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              removed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS mirror_commits (
              mirror_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              git_commit TEXT NOT NULL,
              direction TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (mirror_id, snapshot_id, git_commit, direction)
            );
            CREATE TABLE IF NOT EXISTS merge_rules (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              name TEXT NOT NULL,
              path_globs_json TEXT NOT NULL,
              exclude_globs_json TEXT NOT NULL DEFAULT '[]',
              allow_json TEXT NOT NULL,
              max_files INTEGER,
              status TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              removed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS advisories (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              status TEXT NOT NULL,
              disclosure_state TEXT NOT NULL,
              affected_packages_json TEXT NOT NULL,
              trusted_reviewers_json TEXT NOT NULL,
              private_changesets_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              disclosed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS releases (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              status TEXT NOT NULL,
              target_snapshot_id TEXT,
              included_changesets_json TEXT NOT NULL,
              packages_json TEXT NOT NULL,
              artifacts_json TEXT NOT NULL,
              checks_json TEXT NOT NULL,
              advisories_disclosed_json TEXT NOT NULL,
              rollback_target TEXT,
              policy_decisions_json TEXT NOT NULL DEFAULT '[]',
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        self.migrate_db()
        self.conn.commit()

    def migrate_db(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(changesets)").fetchall()}
        if "workspace_id" not in columns:
            self.conn.execute("ALTER TABLE changesets ADD COLUMN workspace_id TEXT")
        workspace_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(workspaces)").fetchall()}
        if "materialized_snapshot_id" not in workspace_columns:
            self.conn.execute("ALTER TABLE workspaces ADD COLUMN materialized_snapshot_id TEXT")
        if "hidden_paths_json" not in workspace_columns:
            self.conn.execute("ALTER TABLE workspaces ADD COLUMN hidden_paths_json TEXT NOT NULL DEFAULT '[]'")
        if "advisory_id" not in workspace_columns:
            self.conn.execute("ALTER TABLE workspaces ADD COLUMN advisory_id TEXT")
        changeset_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(changesets)").fetchall()}
        if "hidden_paths_json" not in changeset_columns:
            self.conn.execute("ALTER TABLE changesets ADD COLUMN hidden_paths_json TEXT NOT NULL DEFAULT '[]'")
        if "stack_parent_id" not in changeset_columns:
            self.conn.execute("ALTER TABLE changesets ADD COLUMN stack_parent_id TEXT")
        if "advisory_id" not in changeset_columns:
            self.conn.execute("ALTER TABLE changesets ADD COLUMN advisory_id TEXT")
        release_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(releases)").fetchall()}
        if "policy_decisions_json" not in release_columns:
            self.conn.execute("ALTER TABLE releases ADD COLUMN policy_decisions_json TEXT NOT NULL DEFAULT '[]'")
        if "replacement_changeset_id" not in changeset_columns:
            self.conn.execute("ALTER TABLE changesets ADD COLUMN replacement_changeset_id TEXT")
        if "lifecycle_json" not in changeset_columns:
            self.conn.execute("ALTER TABLE changesets ADD COLUMN lifecycle_json TEXT NOT NULL DEFAULT '{}'")
        check_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(check_runs)").fetchall()}
        if "rerun_of" not in check_columns:
            self.conn.execute("ALTER TABLE check_runs ADD COLUMN rerun_of TEXT")
        if "actor" not in check_columns:
            self.conn.execute("ALTER TABLE check_runs ADD COLUMN actor TEXT NOT NULL DEFAULT 'actor_local'")
        if "cost_json" not in check_columns:
            self.conn.execute("ALTER TABLE check_runs ADD COLUMN cost_json TEXT NOT NULL DEFAULT '{}'")
        if "assumptions_json" not in check_columns:
            self.conn.execute("ALTER TABLE check_runs ADD COLUMN assumptions_json TEXT NOT NULL DEFAULT '[]'")
        if "materialized_directory_json" not in check_columns:
            self.conn.execute("ALTER TABLE check_runs ADD COLUMN materialized_directory_json TEXT NOT NULL DEFAULT '{}'")
        if "environment_policy_json" not in check_columns:
            self.conn.execute("ALTER TABLE check_runs ADD COLUMN environment_policy_json TEXT NOT NULL DEFAULT '{}'")
        task_run_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(task_runs)").fetchall()}
        if "assumptions_json" not in task_run_columns:
            self.conn.execute("ALTER TABLE task_runs ADD COLUMN assumptions_json TEXT NOT NULL DEFAULT '[]'")
        for table in ("blobs", "trees"):
            object_columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column in ("last_referenced_at", "purged_at"):
                if column not in object_columns:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        disk_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(disk_leases)").fetchall()}
        if "env_keys_json" not in disk_columns:
            self.conn.execute("ALTER TABLE disk_leases ADD COLUMN env_keys_json TEXT NOT NULL DEFAULT '[]'")
        if "sparse_paths_json" not in disk_columns:
            self.conn.execute("ALTER TABLE disk_leases ADD COLUMN sparse_paths_json TEXT")
        merge_rule_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(merge_rules)").fetchall()}
        if "verify_check_set_id" not in merge_rule_columns:
            self.conn.execute("ALTER TABLE merge_rules ADD COLUMN verify_check_set_id TEXT")
        lease_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(materialization_leases)").fetchall()}
        for column, ddl in (
            ("backend", "TEXT"),
            ("degraded", "INTEGER"),
            ("baseline_path", "TEXT"),
            ("ttl_seconds", "INTEGER"),
            ("attention_json", "TEXT"),
            ("reaped_at", "TEXT"),
            ("salvage_id", "TEXT"),
            ("env_keys_json", "TEXT"),
            ("root_identity", "TEXT"),
            ("sparse_paths_json", "TEXT"),
        ):
            if column not in lease_columns:
                self.conn.execute(f"ALTER TABLE materialization_leases ADD COLUMN {column} {ddl}")
        if "grandfathered" not in lease_columns:
            # Mounts created before leases could expire keep their old contract: never auto-reaped.
            self.conn.execute("ALTER TABLE materialization_leases ADD COLUMN grandfathered INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE materialization_leases SET grandfathered = 1")
        task_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "body" not in task_columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN body TEXT NOT NULL DEFAULT ''")
        if "acceptance_criteria_json" not in task_columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN acceptance_criteria_json TEXT NOT NULL DEFAULT '[]'")
        if "required_checks_json" not in task_columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN required_checks_json TEXT NOT NULL DEFAULT '[]'")
        if "lifecycle_json" not in task_columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN lifecycle_json TEXT NOT NULL DEFAULT '{}'")

    def unpack_db(self) -> None:
        packed = self.packed_db_path.read_bytes()
        if packed.startswith(PACKED_DB_MAGIC):
            raw = zlib.decompress(packed[len(PACKED_DB_MAGIC) :])
        else:
            raw = zlib.decompress(packed)
        tmp = self.db_path.with_name(f"{self.db_path.name}.unpacking")
        tmp.write_bytes(raw)
        os.replace(tmp, self.db_path)

    def pack_db(self) -> None:
        if not self.db_path.exists():
            return
        raw = self.db_path.read_bytes()
        if self.pack_db_max_bytes is not None and len(raw) > self.pack_db_max_bytes:
            if self.packed_db_path.exists():
                self.packed_db_path.unlink()
            return
        packed = PACKED_DB_MAGIC + zlib.compress(raw, level=1)
        if len(packed) >= len(raw):
            if self.packed_db_path.exists():
                self.packed_db_path.unlink()
            return
        tmp = self.packed_db_path.with_name(f"{self.packed_db_path.name}.tmp")
        tmp.write_bytes(packed)
        os.replace(tmp, self.packed_db_path)
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(f"{self.db_path}{suffix}")
            if sidecar.exists():
                sidecar.unlink()

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.conn.close()
        if self.pack_on_close and self.lock_file is not None:
            try:
                self.admin_lock.acquire(exclusive=True)
                try:
                    # Drop our open lock, then pack only if nobody else has the
                    # store open; a Store that opens meanwhile keeps it unpacked.
                    self.lock_file.release()
                    if self.lock_file.acquire(exclusive=True, blocking=False):
                        self.pack_db()
                finally:
                    self.admin_lock.release()
            except OSError:
                pass
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None
        self.admin_lock.close()

    def row(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def put_blob(self, data: bytes) -> dict[str, Any]:
        digest = sha256(data)
        blob_id = f"blob_{digest[:16]}"
        object_path = self.blobs / digest[:2] / digest
        object_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the row first: the write lock it takes keeps GC from purging this
        # blob between our existence check and the commit that references it.
        now = utcnow()
        inserted = self.conn.execute(
            """
            INSERT OR IGNORE INTO blobs (id, hash, size, object_path, created_at, last_referenced_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (blob_id, digest, len(data), "", now, now),
        ).rowcount
        was_purged = False
        if not inserted:
            row = self.conn.execute("SELECT purged_at FROM blobs WHERE id = ?", (blob_id,)).fetchone()
            was_purged = bool(row and row["purged_at"])
            self.conn.execute("UPDATE blobs SET last_referenced_at = ?, purged_at = NULL WHERE id = ?", (now, blob_id))
        if was_purged or not object_path.exists():
            compressed = zlib.compress(data)
            tmp = object_path.with_name(f".{object_path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(COMPRESSED_BLOB_MAGIC + compressed if len(compressed) + len(COMPRESSED_BLOB_MAGIC) < len(data) else data)
            os.replace(tmp, object_path)
        self._blob_cache[blob_id] = data
        return {"id": blob_id, "hash": digest, "size": len(data), "object_path": str(object_path)}

    def read_blob(self, blob_id: str) -> bytes:
        cached = self._blob_cache.get(blob_id)
        if cached is not None:
            return cached
        row = self.row("SELECT hash, object_path, purged_at FROM blobs WHERE id = ?", (blob_id,))
        if row is None:
            raise TriError("path_not_found", "Blob not found.", object=blob_id)
        if row["purged_at"]:
            raise TriError("content_purged", "Blob content was removed by garbage collection.", object=blob_id, purged_at=row["purged_at"])
        stored_path = row["object_path"]
        path = Path(stored_path) if stored_path else self.blobs / row["hash"][:2] / row["hash"]
        if stored_path and not path.is_absolute():
            path = self.blobs / stored_path
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise TriError("path_not_found", "Blob content file is missing from the object store.", object=blob_id) from None
        if data.startswith(COMPRESSED_BLOB_MAGIC):
            data = zlib.decompress(data[len(COMPRESSED_BLOB_MAGIC) :])
        self._blob_cache[blob_id] = data
        return data

    def put_tree(self, entries: dict[str, dict[str, Any]]) -> dict[str, str]:
        """Store ``entries`` as directory-level nodes; unchanged directories are shared."""
        tree_id, digest, nodes = build_tree_nodes(entries)
        self._store_tree_rows(nodes)
        return {"id": tree_id, "hash": digest}

    def _store_tree_rows(self, nodes: list[tuple[str, str, str]]) -> None:
        """Write tree nodes in bulk: one lookup, inserts for new nodes, one touch
        (which also protects them from a concurrent GC) for existing ones."""
        unique = {tree_id: (digest, stored) for tree_id, digest, stored in nodes}
        ids = list(unique)
        existing: dict[str, Any] = {}
        for offset in range(0, len(ids), 900):
            chunk = ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.conn.execute(f"SELECT id, purged_at FROM trees WHERE id IN ({placeholders})", chunk):
                existing[row["id"]] = row["purged_at"]
        now = utcnow()
        missing = [(tree_id, unique[tree_id][0], unique[tree_id][1], now, now) for tree_id in ids if tree_id not in existing]
        if missing:
            self.conn.executemany("INSERT OR IGNORE INTO trees (id, hash, entries_json, created_at, last_referenced_at) VALUES (?, ?, ?, ?, ?)", missing)
        for tree_id, purged_at in existing.items():
            if purged_at:
                self._store_tree_row(tree_id, unique[tree_id][0], unique[tree_id][1])
        live = [tree_id for tree_id, purged_at in existing.items() if not purged_at]
        for offset in range(0, len(live), 900):
            chunk = live[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            self.conn.execute(f"UPDATE trees SET last_referenced_at = ? WHERE id IN ({placeholders})", (now, *chunk))

    def _store_tree_row(self, tree_id: str, digest: str, stored_entries: str) -> None:
        now = utcnow()
        inserted = self.conn.execute(
            """
            INSERT OR IGNORE INTO trees (id, hash, entries_json, created_at, last_referenced_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tree_id, digest, stored_entries, now, now),
        ).rowcount
        if not inserted:
            # Re-referencing a tree also restores content GC may have purged.
            self.conn.execute(
                "UPDATE trees SET last_referenced_at = ?, entries_json = CASE WHEN purged_at IS NULL THEN entries_json ELSE ? END, purged_at = NULL WHERE id = ?",
                (now, stored_entries, tree_id),
            )

    def _tree_row_text(self, tree_id: str) -> str:
        row = self.row("SELECT entries_json, purged_at FROM trees WHERE id = ?", (tree_id,))
        if row is None:
            raise TriError("path_not_found", "Tree not found.", object=tree_id)
        if row["purged_at"]:
            raise TriError("content_purged", "Tree content was removed by garbage collection.", object=tree_id, purged_at=row["purged_at"])
        return row["entries_json"]

    def _resolve_tree(self, tree_id: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
        cached = self._tree_cache.get(tree_id)
        if cached is not None:
            return cached
        text = self._tree_row_text(tree_id)
        if is_hierarchical(text):
            resolved = flatten_tree(tree_id, lambda node_id: decode_node(self._tree_row_text(node_id)))
        else:
            resolved = (decode_tree_json(text), {tree_id})
        self._tree_cache[tree_id] = resolved
        return resolved

    def load_tree(self, tree_id: str) -> dict[str, dict[str, Any]]:
        resolved, _ = self._resolve_tree(tree_id)
        entries = {path: dict(entry) for path, entry in resolved.items()}
        missing = sorted({entry["blob"] for entry in entries.values() if "hash" not in entry and isinstance(entry.get("blob"), str)})
        if missing:
            hashes: dict[str, str] = {}
            for offset in range(0, len(missing), 900):
                chunk = missing[offset : offset + 900]
                placeholders = ",".join("?" for _ in chunk)
                hashes.update({row["id"]: row["hash"] for row in self.rows(f"SELECT id, hash FROM blobs WHERE id IN ({placeholders})", tuple(chunk))})
            for entry in entries.values():
                blob = entry.get("blob")
                if "hash" not in entry and isinstance(blob, str) and blob in hashes:
                    entry["hash"] = hashes[blob]
        return entries

    def collect_references(self, roots: list[str]) -> tuple[set[str], set[str]]:
        """Tree nodes and blobs reachable from ``roots``, visiting each node once.

        Never flattens whole snapshots, so marking cost follows the number of
        distinct nodes rather than snapshots x files. A live root whose content is
        missing aborts (GC must fail closed); already-purged content is skipped."""
        visited: set[str] = set()
        blobs: set[str] = set()
        stack = list(roots)
        while stack:
            tree_id = stack.pop()
            if tree_id in visited:
                continue
            visited.add(tree_id)
            try:
                text = self._tree_row_text(tree_id)
            except TriError as exc:
                if exc.diagnostic.get("code") == "content_purged":
                    continue
                raise
            if is_hierarchical(text):
                node = decode_node(text)
                blobs.update(entry["blob"] for entry in node.get("f", {}).values() if isinstance(entry.get("blob"), str))
                stack.extend(child for child in node.get("d", {}).values() if child not in visited)
            else:
                blobs.update(entry["blob"] for entry in decode_tree_json(text).values() if isinstance(entry.get("blob"), str))
        return visited, blobs

    def tree_references(self, tree_id: str) -> tuple[set[str], set[str]]:
        """Tree (node) ids and blob ids reachable from ``tree_id``."""
        entries, nodes = self._resolve_tree(tree_id)
        return set(nodes), {entry["blob"] for entry in entries.values() if isinstance(entry.get("blob"), str)}

    def load_snapshot_tree(self, snapshot_id: str) -> dict[str, dict[str, Any]]:
        snapshot = self.row("SELECT root_tree_id FROM snapshots WHERE id = ?", (snapshot_id,))
        if snapshot is None:
            raise TriError("path_not_found", "Snapshot not found.", object=snapshot_id)
        return self.load_tree(snapshot["root_tree_id"])

    def create_snapshot(
        self,
        project_id: str,
        entries: dict[str, dict[str, Any]],
        *,
        parents: list[str],
        source: str,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        tree = self.put_tree(entries)
        fingerprint = sha256(dumps({"tree": tree["hash"], "parents": parents, "source": source}).encode())
        snapshot_id = new_id("snap")
        self.conn.execute(
            """
            INSERT INTO snapshots (
              id, project_id, root_tree_id, parent_ids_json, source,
              content_fingerprint, created_by, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                project_id,
                tree["id"],
                dumps(parents),
                source,
                f"sha256:{fingerprint}",
                actor,
                utcnow(),
                dumps(metadata or {}),
            ),
        )
        return {"id": snapshot_id, "root_tree": tree["id"], "content_fingerprint": f"sha256:{fingerprint}"}

    def create_event(
        self,
        *,
        kind: str,
        actor: str,
        event_id: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
        task_id: str | None = None,
        object_refs: dict[str, Any] | None = None,
        input_fingerprints: dict[str, Any] | None = None,
        output_fingerprints: dict[str, Any] | None = None,
        policy_result: dict[str, Any] | None = None,
    ) -> str:
        event_id = event_id or new_id("evt")
        self.conn.execute(
            """
            INSERT INTO events (
              id, actor, project_id, workspace_id, task_id, kind, object_refs_json,
              input_fingerprints_json, output_fingerprints_json, policy_result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                actor,
                project_id,
                workspace_id,
                task_id,
                kind,
                dumps(object_refs or {}),
                dumps(input_fingerprints or {}),
                dumps(output_fingerprints or {}),
                dumps(policy_result or {"allowed": True}),
                utcnow(),
            ),
        )
        return event_id

    def get_project(self, name_or_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM projects WHERE id = ? OR name = ?", (name_or_id, name_or_id))
        if row is None:
            raise TriError("path_not_found", "Project not found.", object=name_or_id)
        return row

    def get_workspace(self, workspace_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        if row is None:
            raise TriError("path_not_found", "Workspace not found.", object=workspace_id)
        return row

    def get_task(self, task_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise TriError("path_not_found", "Task not found.", object=task_id)
        return row

    def get_changeset(self, changeset_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM changesets WHERE id = ?", (changeset_id,))
        if row is None:
            raise TriError("path_not_found", "Changeset not found.", object=changeset_id)
        return row

    def get_review(self, review_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if row is None:
            raise TriError("path_not_found", "Review not found.", object=review_id)
        return row

    def get_artifact(self, artifact_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if row is None:
            raise TriError("path_not_found", "Artifact not found.", object=artifact_id)
        return row

    def get_workspace_conflict(self, conflict_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM workspace_conflicts WHERE id = ?", (conflict_id,))
        if row is None:
            raise TriError("path_not_found", "Workspace conflict not found.", object=conflict_id)
        return row

    def get_secret(self, secret_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM secret_refs WHERE id = ?", (secret_id,))
        if row is None:
            raise TriError("path_not_found", "Secret not found.", object=secret_id)
        return row

    def get_secret_lease(self, lease_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM secret_leases WHERE id = ?", (lease_id,))
        if row is None:
            raise TriError("path_not_found", "Secret lease not found.", object=lease_id)
        return row

    def get_materialization_lease(self, lease_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM materialization_leases WHERE id = ?", (lease_id,))
        if row is None:
            raise TriError("path_not_found", "Materialization lease not found.", object=lease_id)
        return row

    def get_policy_grant(self, grant_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM policy_grants WHERE id = ?", (grant_id,))
        if row is None:
            raise TriError("path_not_found", "Policy grant not found.", object=grant_id)
        return row

    def get_advisory(self, advisory_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM advisories WHERE id = ?", (advisory_id,))
        if row is None:
            raise TriError("path_not_found", "Advisory not found.", object=advisory_id)
        return row

    def get_release(self, release_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM releases WHERE id = ?", (release_id,))
        if row is None:
            raise TriError("path_not_found", "Release not found.", object=release_id)
        return row

    def get_export(self, export_id: str) -> sqlite3.Row:
        row = self.row("SELECT * FROM exports WHERE id = ?", (export_id,))
        if row is None:
            raise TriError("path_not_found", "Export not found.", object=export_id)
        return row

    def record_policy_decision(
        self,
        *,
        actor: str,
        action: str,
        object_id: str,
        decision: dict[str, Any],
        project_id: str | None = None,
        workspace_id: str | None = None,
        task_id: str | None = None,
        kind: str = "policy.evaluated",
    ) -> str:
        evt = new_id("evt")
        decision["event"] = evt
        with self.conn:
            self.create_event(
                kind=kind,
                actor=actor,
                event_id=evt,
                project_id=project_id,
                workspace_id=workspace_id,
                task_id=task_id,
                object_refs={"decision": decision["id"], "action": action, "object": object_id},
                policy_result=decision,
            )
            if object_id.startswith("cs_"):
                row = self.row("SELECT policy_decisions_json FROM changesets WHERE id = ?", (object_id,))
                if row is not None:
                    decisions = loads(row["policy_decisions_json"], [])
                    decisions.append(decision["id"])
                    self.conn.execute("UPDATE changesets SET policy_decisions_json = ? WHERE id = ?", (dumps(decisions), object_id))
        return evt


def actor_task_scope(actor: str) -> str | None:
    if not actor.startswith("task:"):
        return None
    task_id = actor.split(":", 1)[1].strip()
    return task_id or None


def actor_identity_known(actor: str) -> bool:
    return actor_task_scope(actor) is not None or actor == "actor_local" or actor.startswith(
        (
            "agent:",
            "agent_",
            "human:",
            "human_",
            "service:",
            "service_",
            "actor_",
        )
    )


def require_known_actor(actor: str) -> None:
    if not actor_identity_known(actor):
        raise TriError(
            "unknown_actor",
            "Actor identity is not recognized.",
            actor=actor,
            accepted_formats=ACTOR_ACCEPTED_FORMATS,
        )


def require_not_task_scoped_actor(actor: str, *, action: str, object: str | None = None) -> None:
    task_scope = actor_task_scope(actor)
    if task_scope is None:
        return
    raise TriError(
        "policy_denied",
        "Task-scoped actors cannot create project-level objects.",
        action=action,
        object=object,
        task_scope=task_scope,
        required_grant="non_task_scoped_actor",
    )


def actor_selectors(actor: str) -> list[str]:
    require_known_actor(actor)
    selectors = {"role:anyone", f"actor:{actor}"}
    task_scope = actor_task_scope(actor)
    if task_scope is not None:
        selectors.add(f"task:{task_scope}")
    if actor == "actor_local":
        selectors.update({"role:maintainer", "role:security"})
    if actor.startswith("agent:") or actor.startswith("agent_"):
        selectors.add("role:agent")
    if actor.startswith("human:") or actor.startswith("human_"):
        selectors.add("role:human")
    if actor.startswith("service:") or actor.startswith("service_"):
        selectors.add("role:service")
    return sorted(selectors)


def actor_kind(actor: str) -> str:
    if actor_task_scope(actor) is not None:
        return "task"
    if actor == "actor_local":
        return "local"
    if actor.startswith("agent:") or actor.startswith("agent_"):
        return "agent"
    if actor.startswith("human:") or actor.startswith("human_"):
        return "human"
    if actor.startswith("service:") or actor.startswith("service_"):
        return "service"
    if actor.startswith("actor_"):
        return "actor"
    return "unknown"


def actor_identity_data(actor: str) -> dict[str, Any]:
    require_known_actor(actor)
    task_scope = actor_task_scope(actor)
    selectors = actor_selectors(actor)
    roles = sorted(selector for selector in selectors if selector.startswith("role:"))
    return {
        "id": actor,
        "kind": actor_kind(actor),
        "known": True,
        "task_scope": task_scope,
        "principal": f"task:{task_scope}" if task_scope else f"actor:{actor}",
        "actor_selector": f"actor:{actor}",
        "selectors": selectors,
        "roles": roles,
        "capabilities": {
            "project_level": task_scope is None,
            "task_scoped": task_scope is not None,
            "maintainer": "role:maintainer" in selectors,
            "security": "role:security" in selectors,
            "can_create_project_objects": task_scope is None,
        },
        "accepted_formats": ACTOR_ACCEPTED_FORMATS,
    }


def normalize_principal(value: str) -> str:
    value = value.strip()
    if not value:
        raise TriError("invalid_command", "Policy principal cannot be empty.")
    if value.startswith("actor:") or value.startswith("role:") or value.startswith("task:"):
        return value
    return f"actor:{value}"


def action_matches(granted_actions: list[str], action: str) -> bool:
    return "*" in granted_actions or action in granted_actions


def default_grant_actions(object_type: str) -> list[str]:
    if object_type == "task":
        return ["task.read"]
    if object_type == "package":
        return ["package.read"]
    if object_type == "workspace":
        return ["workspace.read"]
    if object_type == "changeset":
        return ["changeset.read"]
    if object_type == "review":
        return ["review.read"]
    if object_type == "advisory":
        return ["advisory.read"]
    if object_type == "release":
        return ["release.read"]
    if object_type == "secret":
        return ["secret.read"]
    return ["read"]


def object_policy_context(store: Store, object_id: str) -> dict[str, Any]:
    if object_id.startswith("task_"):
        task = store.get_task(object_id)
        return {
            "type": "task",
            "id": task["id"],
            "project": task["project_id"],
            "workspace": None,
            "task": task["id"],
            "visibility": ["public"],
            "owner": task["requester"],
        }
    if object_id.startswith("pkg_"):
        row = store.row("SELECT * FROM package_boundaries WHERE id = ?", (object_id,))
        if row is None:
            raise TriError("path_not_found", "Package boundary not found.", object=object_id)
        return {
            "type": "package",
            "id": row["id"],
            "project": row["project_id"],
            "workspace": None,
            "task": None,
            "visibility": loads(row["visibility_json"], []),
            "owner": row["created_by"],
            "owners": loads(row["owners_json"], []),
            "path_globs": loads(row["path_globs_json"], []),
        }
    if object_id.startswith("ws_"):
        ws = store.get_workspace(object_id)
        return {
            "type": "workspace",
            "id": ws["id"],
            "project": ws["project_id"],
            "workspace": ws["id"],
            "task": ws["task_id"],
            "visibility": loads(ws["visibility_json"], []),
            "owner": ws["owner_actor"],
        }
    if object_id.startswith("cs_"):
        cs = store.get_changeset(object_id)
        owner = None
        task_id = None
        if cs["workspace_id"]:
            ws = store.row("SELECT owner_actor, task_id FROM workspaces WHERE id = ?", (cs["workspace_id"],))
            if ws:
                owner = ws["owner_actor"]
                task_id = ws["task_id"]
        if owner is None and cs["task_run_id"]:
            run = store.row("SELECT actor, task_id FROM task_runs WHERE id = ?", (cs["task_run_id"],))
            if run:
                owner = run["actor"]
                task_id = task_id or run["task_id"]
        return {
            "type": "changeset",
            "id": cs["id"],
            "project": cs["project_id"],
            "workspace": cs["workspace_id"],
            "task": task_id,
            "visibility": loads(cs["visibility_json"], []),
            "owner": owner,
        }
    if object_id.startswith("review_"):
        review = store.get_review(object_id)
        ctx = object_policy_context(store, review["changeset_id"])
        return {**ctx, "type": "review", "id": review["id"], "changeset": review["changeset_id"]}
    if object_id.startswith("adv_"):
        advisory = store.get_advisory(object_id)
        visibility = ["public"] if advisory["disclosure_state"] in ("public", "disclosed") else ["embargoed"]
        return {
            "type": "advisory",
            "id": advisory["id"],
            "project": advisory["project_id"],
            "workspace": None,
            "visibility": visibility,
            "owner": advisory["created_by"],
            "trusted_reviewers": loads(advisory["trusted_reviewers_json"], []),
        }
    if object_id.startswith("rel_"):
        release = store.get_release(object_id)
        return {
            "type": "release",
            "id": release["id"],
            "project": release["project_id"],
            "workspace": None,
            "visibility": ["public"],
            "owner": release["created_by"],
        }
    if object_id.startswith("secret_"):
        secret = store.get_secret(object_id)
        return {
            "type": "secret",
            "id": secret["id"],
            "project": secret["project_id"],
            "workspace": None,
            "visibility": ["private"],
            "owner": secret["created_by"],
        }
    raise TriError("path_not_found", "Unsupported policy object.", object=object_id)


def grant_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": row["project_id"],
        "object_type": row["object_type"],
        "object": row["object_id"],
        "principal": row["principal"],
        "actions": loads(row["actions_json"], []),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


def require_policy_grant_inspect_access(store: Store, grant: sqlite3.Row, *, actor: str) -> None:
    selectors = actor_selectors(actor)
    if "role:maintainer" in selectors or grant["created_by"] == actor or grant["principal"] in selectors:
        return
    ctx = object_policy_context(store, grant["object_id"])
    decision, _ = object_access_decision(store, grant["object_id"], actor=actor, action=default_grant_actions(ctx["type"])[0])
    if decision["allowed"]:
        return
    raise TriError(
        "policy_denied",
        "Actor is not allowed to inspect this policy grant.",
        object=grant["id"],
        action="grant.read",
        required_grant="grant principal or object read",
    )


def active_grants_for_object(store: Store, object_id: str) -> list[dict[str, Any]]:
    return [grant_data(row) for row in store.rows("SELECT * FROM policy_grants WHERE object_id = ? AND status = ? ORDER BY created_at", (object_id, "active"))]


def inherited_grants_for_object(store: Store, ctx: dict[str, Any], *, action: str) -> list[dict[str, Any]]:
    workspace_id = ctx.get("workspace")
    if not workspace_id or ctx["type"] == "workspace" or not action.endswith(".read"):
        return []
    grants = []
    for grant in active_grants_for_object(store, workspace_id):
        if action_matches(grant["actions"], "workspace.read"):
            grants.append({**grant, "inherited_from": workspace_id, "inherited_action": "workspace.read"})
    return grants


def object_access_decision(store: Store, object_id: str, *, actor: str, action: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ctx = object_policy_context(store, object_id)
    visibility = ctx["visibility"] or ["private"]
    selectors = actor_selectors(actor)
    task_scope = actor_task_scope(actor)
    task_scope_mismatch = task_scope is not None and ctx["type"] in ("workspace", "changeset", "review") and ctx.get("task") != task_scope
    grants = []
    allowed = False
    reason = "missing_allow"
    if task_scope_mismatch:
        reason = "task_scope_mismatch"
    if not task_scope_mismatch and task_scope is not None and ctx.get("task") == task_scope:
        if ctx["type"] == "task" and action in ("task.read", "task.run"):
            allowed = True
            reason = "task_scope"
            grants.append(f"task:{task_scope}")
        elif ctx["type"] == "workspace" and action.startswith("workspace."):
            allowed = True
            reason = "task_scope"
            grants.append(f"task:{task_scope}")
        elif ctx["type"] == "changeset" and action.startswith("changeset."):
            allowed = True
            reason = "task_scope"
            grants.append(f"task:{task_scope}")
        elif ctx["type"] == "review" and action.startswith("review."):
            allowed = True
            reason = "task_scope"
            grants.append(f"task:{task_scope}")
    if not task_scope_mismatch and "public" in visibility and action.endswith(".read"):
        allowed = True
        reason = "public_visibility"
        grants.append("public_visibility")
    if not task_scope_mismatch and ctx["type"] == "task" and action == "task.run" and "public" in visibility and "role:agent" in selectors:
        allowed = True
        reason = "public_task_run"
        grants.append("public_task_run")
    if not task_scope_mismatch and actor == ctx["owner"]:
        allowed = True
        reason = "owner"
        grants.append("owner")
    if not task_scope_mismatch and "role:maintainer" in selectors:
        allowed = True
        reason = "maintainer"
        grants.append("role:maintainer")
    if not task_scope_mismatch and ctx["type"] == "advisory" and action.endswith(".read"):
        trusted = set(ctx.get("trusted_reviewers", []))
        if trusted.intersection(selectors):
            allowed = True
            reason = "trusted_reviewer"
            grants.append("trusted_reviewer")
    if not task_scope_mismatch and ctx["type"] == "package" and action.endswith(".read"):
        package_owners = set(ctx.get("owners", []))
        matched_owners = package_owners.intersection(selectors)
        if matched_owners:
            allowed = True
            reason = "package_owner"
            grants.extend(sorted(matched_owners))
        if "embargoed" in visibility and "role:security" in selectors:
            allowed = True
            reason = "security_role"
            grants.append("role:security")
    matched_grants = []
    if not task_scope_mismatch:
        for grant in active_grants_for_object(store, object_id):
            if grant["principal"] in selectors and action_matches(grant["actions"], action):
                allowed = True
                reason = "explicit_grant"
                grants.append(grant["id"])
                matched_grants.append(grant)
        inherited_grants = inherited_grants_for_object(store, ctx, action=action)
        for grant in inherited_grants:
            if grant["principal"] in selectors:
                allowed = True
                reason = "inherited_grant"
                grants.append(grant["id"])
                matched_grants.append(grant)
    else:
        inherited_grants = []
    decision = {
        "id": new_id("decision"),
        "allowed": allowed,
        "actor": actor,
        "action": action,
        "object": object_id,
        "object_type": ctx["type"],
        "policies": ["local_policy_v0"],
        "visibility": visibility,
        "owner": ctx["owner"],
        "task": ctx.get("task"),
        "task_scope": task_scope,
        "actor_selectors": selectors,
        "grants": sorted(set(grants)),
        "matched_grants": matched_grants,
        "denials": [] if allowed else ["policy_denied"],
        "redactions": [],
        "reason_code": "allowed" if allowed else "policy_denied",
        "reason": reason,
        "event": None,
    }
    diagnostics = [] if allowed else [
        decision_denial(
            "policy_denied",
            "Actor is not allowed to access this object.",
            object=object_id,
            action=action,
            visibility=visibility,
            required_grant=action,
        )
    ]
    return decision, diagnostics


def require_object_access(store: Store, object_id: str, *, actor: str, action: str) -> dict[str, Any]:
    decision, diagnostics = object_access_decision(store, object_id, actor=actor, action=action)
    if diagnostics:
        ctx = object_policy_context(store, object_id)
        evt = store.record_policy_decision(
            actor=actor,
            action=action,
            object_id=object_id,
            decision=decision,
            project_id=ctx["project"],
            workspace_id=ctx.get("workspace"),
            task_id=ctx.get("task"),
            kind="policy.denied",
        )
        raise TriError(
            "policy_denied",
            "Actor is not allowed to access this object.",
            object=object_id,
            action=action,
            visibility=decision["visibility"],
            required_grant=action,
            decision=decision["id"],
            event=evt,
            explain_command=["tri", "policy", "explain", decision["id"]],
        )
    return decision


def policy_provenance_data(decision: dict[str, Any]) -> dict[str, Any]:
    matched_grants = []
    for grant in decision.get("matched_grants", []):
        matched = {
            "id": grant.get("id"),
            "object": grant.get("object"),
            "principal": grant.get("principal"),
            "actions": grant.get("actions", []),
        }
        if grant.get("inherited_from"):
            matched["inherited_from"] = grant["inherited_from"]
            matched["inherited_action"] = grant.get("inherited_action")
        matched_grants.append(matched)
    return {
        "decision": decision.get("id"),
        "event": decision.get("event"),
        "allowed": decision.get("allowed"),
        "action": decision.get("action"),
        "object": decision.get("object"),
        "reason": decision.get("reason"),
        "reason_code": decision.get("reason_code"),
        "grants": decision.get("grants", []),
        "matched_grants": matched_grants,
    }


REF_PREFIXES: list[tuple[str, str]] = [
    ("actor", "actors"),
    ("pkg", "packages"),
    ("checkset", "check_sets"),
    ("artifact", "artifacts"),
    ("changeset", "changesets"),
    ("conflict", "conflicts"),
    ("decision", "decisions"),
    ("export", "exports"),
    ("review", "reviews"),
    ("secret", "secrets"),
    ("target", "targets"),
    ("check", "checks"),
    ("grant", "grants"),
    ("lease", "leases"),
    ("proj", "projects"),
    ("revact", "review_actions"),
    ("task", "tasks"),
    ("tree", "trees"),
    ("blob", "blobs"),
    ("snap", "snapshots"),
    ("run", "task_runs"),
    ("evt", "events"),
    ("rel", "releases"),
    ("adv", "advisories"),
    ("op", "operations"),
    ("cs", "changesets"),
    ("ws", "workspaces"),
]


OBJECT_INSPECT_COMMANDS: list[tuple[str, str, list[str]]] = [
    ("actor_", "actor", ["actors", "inspect"]),
    ("pkg_", "package", ["packages", "inspect"]),
    ("checkset_", "check_set", ["checks", "set-inspect"]),
    ("artifact_", "artifact", ["artifacts", "inspect"]),
    ("changeset_", "changeset", ["changeset", "inspect"]),
    ("conflict_", "conflict", ["conflicts", "inspect"]),
    ("decision_", "policy_decision", ["policy", "explain"]),
    ("export_", "export", ["export", "inspect"]),
    ("review_", "review", ["review", "inspect"]),
    ("secret_", "secret", ["secrets", "inspect"]),
    ("target_", "target", ["targets", "inspect"]),
    ("check_", "check", ["checks", "inspect"]),
    ("grant_", "grant", ["policy", "grant-inspect"]),
    ("lease_", "lease", ["leases", "inspect"]),
    ("proj_", "project", ["projects", "inspect"]),
    ("revact_", "review_action", ["review", "action-inspect"]),
    ("task_", "task", ["task", "inspect"]),
    ("tree_", "tree", ["trees", "inspect"]),
    ("blob_", "blob", ["blobs", "inspect"]),
    ("snap_", "snapshot", ["snapshots", "inspect"]),
    ("run_", "task_run", ["task", "run-inspect"]),
    ("evt_", "event", ["events", "inspect"]),
    ("rel_", "release", ["release", "inspect"]),
    ("adv_", "advisory", ["advisory", "inspect"]),
    ("op_", "operation", ["operations", "inspect"]),
    ("cs_", "changeset", ["changeset", "inspect"]),
    ("ws_", "workspace", ["workspace", "inspect"]),
]


def object_inspect_command(object_id: str) -> tuple[str, list[str]]:
    for prefix, object_type, argv_prefix in OBJECT_INSPECT_COMMANDS:
        if object_id.startswith(prefix):
            return object_type, [*argv_prefix, object_id]
    raise TriError(
        "invalid_command",
        "Object id does not have a supported inspect command.",
        object=object_id,
        accepted_prefixes=[prefix for prefix, _, _ in OBJECT_INSPECT_COMMANDS],
    )


REF_FIELD_KEYS = {
    "id",
    "actor",
    "actors",
    "object",
    "project",
    "projects",
    "package",
    "packages",
    "package_boundary",
    "package_boundaries",
    "workspace",
    "workspaces",
    "changeset",
    "changesets",
    "source_changeset",
    "replacement_changeset",
    "review",
    "reviews",
    "review_action",
    "check",
    "checks",
    "check_set",
    "check_sets",
    "artifact",
    "artifacts",
    "stdout",
    "stderr",
    "export",
    "exports",
    "release",
    "releases",
    "advisory",
    "advisories",
    "secret",
    "secrets",
    "lease",
    "leases",
    "grant",
    "grants",
    "decision",
    "decisions",
    "target",
    "targets",
    "snapshot",
    "snapshots",
    "base_snapshot",
    "head_snapshot",
    "target_snapshot",
    "previous_snapshot",
    "input_snapshot",
    "output_snapshot",
    "mounted_snapshot",
    "last_sync_snapshot",
    "root_tree",
    "tree",
    "trees",
    "blob",
    "blobs",
    "old_blob",
    "new_blob",
    "base_blob",
    "workspace_blob",
    "target_blob",
    "operation",
    "operations",
    "parent",
    "task",
    "tasks",
    "task_run",
    "task_runs",
    "run",
    "runs",
    "event",
    "events",
}


REF_FREE_TEXT_KEYS = {
    "query",
    "text",
    "body",
    "title",
    "message",
    "description",
    "risk_summary",
    "summary",
    "command",
    "path",
    "paths",
    "target_ref",
    "target_remote",
    "rollback_target",
}


def collect_refs(value: Any) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    def is_object_id(prefix: str, value: str) -> bool:
        suffix = value.removeprefix(f"{prefix}_")
        return len(suffix) == 12 and all(char in "0123456789abcdef" for char in suffix)

    def add(kind: str, object_id: str) -> None:
        if object_id in seen.setdefault(kind, set()):
            return
        seen[kind].add(object_id)
        refs.setdefault(kind, []).append(object_id)

    def visit(item: Any, *, key: str | None = None, force: bool = False) -> None:
        if key in REF_FREE_TEXT_KEYS and not force:
            return
        if isinstance(item, str):
            if not force and key not in REF_FIELD_KEYS:
                return
            for prefix, kind in REF_PREFIXES:
                if item.startswith(f"{prefix}_") and is_object_id(prefix, item):
                    add(kind, item)
                    break
            return
        if isinstance(item, dict):
            for child_key, child in item.items():
                child_force = force or child_key in REF_FIELD_KEYS
                if child_key in REF_FREE_TEXT_KEYS and not child_force:
                    continue
                visit(child, key=child_key, force=child_force)
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child, key=key, force=force)

    visit(value)
    return refs


def merge_refs(*groups: dict[str, Any] | None) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for group in groups:
        for kind, value in (group or {}).items():
            values = value if isinstance(value, list) else [value]
            for object_id in values:
                if not isinstance(object_id, str):
                    continue
                if object_id in seen.setdefault(kind, set()):
                    continue
                seen[kind].add(object_id)
                merged.setdefault(kind, []).append(object_id)
    return merged


def ref_links(refs: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    links: dict[str, dict[str, Any]] = {}
    for ref_kind, object_ids in refs.items():
        for object_id in object_ids:
            if not isinstance(object_id, str) or object_id in links:
                continue
            try:
                object_type, _ = object_inspect_command(object_id)
            except TriError:
                continue
            inspect_argv = ["objects", "inspect", object_id]
            links[object_id] = {
                "object": object_id,
                "object_type": object_type,
                "ref_kind": ref_kind,
                "inspect_argv": inspect_argv,
                "inspect_command": ["tri", *inspect_argv],
                "inspect_route": f"/v0/objects/{quote(object_id, safe='')}",
            }
    return {object_id: links[object_id] for object_id in sorted(links)}


def response_mode(command: list[str]) -> str:
    for index, item in enumerate(command):
        if item == "--response" and index + 1 < len(command):
            return command[index + 1]
        if item.startswith("--response="):
            return item.split("=", 1)[1]
    return os.environ.get("TRI_RESPONSE", "full")


def summary_data(schema: str, data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    if schema in ("tri.batch", "tri.api.batch"):
        results = []
        include_result_data = "returns" not in data
        returns_only = data.get("report") == "returns"
        for result in data.get("results", []):
            if not isinstance(result, dict):
                continue
            if returns_only and result.get("ok"):
                continue
            item = {
                key: result[key]
                for key in ("index", "name", "as", "ok", "status", "schema", "wall_ms")
                if key in result
            }
            if include_result_data and "data" in result:
                item["data"] = result["data"]
            if result.get("diagnostics"):
                item["diagnostics"] = result["diagnostics"]
            results.append(item)
        batch_summary = {
            "count": data.get("count", 0),
            "executed": data.get("executed", 0),
            "ok_count": data.get("ok_count", 0),
            "failed": data.get("failed", 0),
            "results": results,
        }
        if "returns" in data:
            batch_summary["returns"] = data["returns"]
        return batch_summary
    if schema == "tri.import":
        target = data.get("target") if isinstance(data.get("target"), dict) else {}
        snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        return {
            "project": data.get("project"),
            "target": {"id": target.get("id"), "name": target.get("name"), "snapshot": target.get("snapshot")},
            "snapshot": {"id": snapshot.get("id"), "root_tree": snapshot.get("root_tree")},
        }
    if schema == "tri.workspace.create":
        return {key: data.get(key) for key in ("id", "project", "head_snapshot", "task", "task_run", "status")}
    if schema == "tri.files.write":
        return {key: data.get(key) for key in ("operation", "snapshot", "paths")}
    if schema == "tri.check.inspect":
        return {
            key: data.get(key)
            for key in (
                "id",
                "workspace",
                "status",
                "snapshot_after",
                "exit_status",
                "duration_ms",
                "file_changes",
            )
        }
    if schema == "tri.changeset.inspect":
        return {
            key: data.get(key)
            for key in (
                "id",
                "project",
                "workspace",
                "title",
                "status",
                "head_snapshot",
                "touched_paths",
                "checks",
                "export_status",
            )
        }
    if schema == "tri.changeset.compare":
        return data
    if schema == "tri.review.create":
        return {key: data.get(key) for key in ("id", "changeset", "status", "reviewers")}
    if schema == "tri.review.action":
        return {key: data.get(key) for key in ("review", "action", "status")}
    keep = (
        "id",
        "name",
        "status",
        "project",
        "workspace",
        "changeset",
        "operation",
        "snapshot",
        "paths",
        "export",
        "record",
        "output",
        "artifact",
        "counts",
    )
    return {key: data[key] for key in keep if key in data}


def summary_command(command: list[str]) -> list[str]:
    if not command:
        return []
    result = [command[0]]
    skip_next = False
    for item in command[1:]:
        if skip_next:
            skip_next = False
            continue
        if item in ("--actor", "--response"):
            skip_next = True
            continue
        if item.startswith("--actor=") or item.startswith("--response="):
            continue
        result.append(item)
        if len(result) >= 3:
            break
    return result


def batch_response_flag_present(argv: list[str]) -> bool:
    for item in argv:
        if item == "--":
            return False
        if item == "--response" or item.startswith("--response="):
            return True
    return False


def stringify_batch_value(value: Any, placeholder: str) -> str:
    if isinstance(value, (dict, list)):
        raise TriError("invalid_command", "Batch placeholder resolved to a non-scalar value.", placeholder=placeholder)
    if value is None:
        return ""
    return str(value)


def lookup_batch_binding(bindings: dict[str, Any], path: str) -> Any:
    parts = path.split(".")
    if not parts or not parts[0]:
        raise TriError("invalid_command", "Batch placeholder is empty.", placeholder=path)
    if parts[0] not in bindings:
        raise TriError("invalid_command", "Batch placeholder references an unknown result.", placeholder=path)
    current: Any = bindings[parts[0]]
    for part in parts[1:]:
        if isinstance(current, dict):
            if part not in current:
                raise TriError("invalid_command", "Batch placeholder path does not exist.", placeholder=path, segment=part)
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise TriError("invalid_command", "Batch placeholder index is out of range.", placeholder=path, segment=part)
            current = current[index]
        else:
            raise TriError("invalid_command", "Batch placeholder cannot traverse this value.", placeholder=path, segment=part)
    return current


def resolve_batch_arg(value: str, bindings: dict[str, Any]) -> str:
    full_match = BATCH_PLACEHOLDER_RE.fullmatch(value)
    if full_match:
        placeholder = full_match.group(1)
        return stringify_batch_value(lookup_batch_binding(bindings, placeholder), placeholder)

    def replace(match: re.Match[str]) -> str:
        placeholder = match.group(1)
        return stringify_batch_value(lookup_batch_binding(bindings, placeholder), placeholder)

    return BATCH_PLACEHOLDER_RE.sub(replace, value)


def resolve_batch_return(value: Any, bindings: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full_match = BATCH_PLACEHOLDER_RE.fullmatch(value)
        if full_match:
            return lookup_batch_binding(bindings, full_match.group(1))

        def replace(match: re.Match[str]) -> str:
            placeholder = match.group(1)
            return stringify_batch_value(lookup_batch_binding(bindings, placeholder), placeholder)

        return BATCH_PLACEHOLDER_RE.sub(replace, value)
    if isinstance(value, list):
        return [resolve_batch_return(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: resolve_batch_return(item, bindings) for key, item in value.items()}
    return value


def result_name_for_batch_item(item: dict[str, Any], argv: list[str], index: int) -> str:
    name = item.get("name")
    if isinstance(name, str) and name:
        return name
    alias = item.get("as")
    if isinstance(alias, str) and alias:
        return alias
    meaningful = [part for part in argv if not part.startswith("--")]
    return ".".join(meaningful[:2]) if meaningful else f"command.{index}"


# Arguments that name a file or directory on the machine running Tri. Over the
# HTTP API that machine is the server: `files write --body /etc/shadow` would
# copy a server file into a workspace, `workspace mount <ws> /srv/...` would
# write into the server's filesystem. Remote callers use the file API instead.
SERVER_PATH_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "cmd_files_write": ("body",),
    "cmd_import": ("repo",),
    "cmd_workspace_mount": ("path",),
    "cmd_export_patch": ("output",),
    "cmd_export_commit": ("repo",),
    "cmd_export_github_pr": ("repo",),
    "cmd_secrets_put": ("value_file",),
    "cmd_task_create": ("body_file",),
    "cmd_advisory_create": ("body_file",),
    "cmd_policy_create_embargo": ("body_file",),
    "cmd_batch": ("plan",),
    "cmd_mirror_add": ("remote",),
}


def split_remote_flag(argv: list[str]) -> tuple[list[str], str | None]:
    """Strip `--remote <url>` from the global options before the command word."""
    out: list[str] = []
    remote = None
    index = 0
    while index < len(argv) and argv[index].startswith("--"):
        part = argv[index]
        if part == "--remote" and index + 1 < len(argv):
            remote = argv[index + 1]
            index += 2
        elif part.startswith("--remote="):
            remote = part.split("=", 1)[1]
            index += 1
        elif part in ("--actor", "--response") and index + 1 < len(argv):
            out += argv[index : index + 2]
            index += 2
        else:
            out.append(part)
            index += 1
    return out + argv[index:], remote


def require_no_server_paths(args: argparse.Namespace, *, actor: str) -> None:
    names = SERVER_PATH_ARGUMENTS.get(getattr(getattr(args, "func", None), "__name__", ""), ())
    used = [name for name in names if getattr(args, name, None) not in (None, "", [])]
    if used and "role:maintainer" not in actor_selectors(actor):
        raise TriError(
            "policy_denied",
            "Arguments that name paths on the server are not available over the API.",
            arguments=["--" + name.replace("_", "-") for name in used],
            reason="server_path",
            hint="Send content through the file API (files write --text or --base64) instead of a server path.",
        )


def run_batch_payload(
    store: Store,
    payload: dict[str, Any],
    *,
    default_actor: str,
    start: float | None = None,
    pinned_actor: str | None = None,
    server_paths_allowed: bool = True,
) -> tuple[dict[str, Any], bool]:
    commands = payload.get("commands")
    if commands is None:
        commands = payload.get("batch")
    if not isinstance(commands, list):
        raise TriError("invalid_command", "Batch request requires commands as a list.")

    continue_on_error = bool(payload.get("continue_on_error", False))
    default_response = payload.get("response")
    if default_response is not None and default_response not in ("full", "summary"):
        raise TriError("invalid_command", "Batch response must be full or summary.")
    report = payload.get("report")
    if report is not None and report not in ("results", "returns"):
        raise TriError("invalid_command", "Batch report must be results or returns.")

    bindings: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    for index, item in enumerate(commands):
        if not isinstance(item, dict):
            raise TriError("invalid_command", "Batch command entries must be objects.", index=index)
        raw_argv = item.get("argv")
        if raw_argv is None:
            raw_argv = item.get("command")
        if not isinstance(raw_argv, list) or not all(isinstance(part, str) for part in raw_argv):
            raise TriError("invalid_command", "Batch command requires argv as a list of strings.", index=index)
        argv = [resolve_batch_arg(part, bindings) for part in raw_argv]
        if argv and argv[0] == "tri":
            argv = argv[1:]
        if argv and argv[0] in API_BLOCKED_COMMANDS:
            raise TriError("invalid_command", "This command is not available in a batch.", object=argv[0], index=index)
        if argv and argv[0] == "batch":
            raise TriError("invalid_command", "Nested batch commands are not supported.", index=index)

        response = item.get("response", default_response)
        if response is not None:
            if response not in ("full", "summary"):
                raise TriError("invalid_command", "Batch command response must be full or summary.", index=index)
            if not batch_response_flag_present(argv):
                argv = ["--response", response, *argv]

        item_actor = item.get("actor", payload.get("actor", default_actor))
        if item_actor is not None and not isinstance(item_actor, str):
            raise TriError("invalid_command", "Batch actor must be a string.", index=index)
        if pinned_actor is not None and item_actor and item_actor != pinned_actor:
            raise_actor_mismatch(item_actor, pinned_actor)
        command_started = time.perf_counter()
        output, status = execute_argv(argv, actor=item_actor or default_actor, start=time.time(), store=store, pinned_actor=pinned_actor, server_paths_allowed=server_paths_allowed)
        wall_ms = round((time.perf_counter() - command_started) * 1000, 3)
        alias = item.get("as")
        if alias is not None and not isinstance(alias, str):
            raise TriError("invalid_command", "Batch alias must be a string.", index=index)
        name = result_name_for_batch_item(item, argv, index)
        result = {
            "index": index,
            "name": name,
            "ok": bool(output.get("ok", False)),
            "status": status,
            "schema": output.get("schema"),
            "wall_ms": wall_ms,
            "data": output.get("data", {}),
            "events": output.get("events", []),
        }
        if alias:
            result["as"] = alias
            bindings[alias] = output
        if output.get("diagnostics"):
            result["diagnostics"] = output.get("diagnostics")
        if output.get("warnings"):
            result["warnings"] = output.get("warnings")
        results.append(result)
        if status and not continue_on_error:
            break

    failed = sum(1 for result in results if not result.get("ok"))
    data = {
        "count": len(commands),
        "executed": len(results),
        "ok_count": sum(1 for result in results if result.get("ok")),
        "failed": failed,
        "results": results,
    }
    if report is not None:
        data["report"] = report
    returns = payload.get("return", payload.get("returns"))
    if returns is not None:
        data["returns"] = resolve_batch_return(returns, bindings)
    return data, failed == 0 and len(results) == len(commands)


def envelope(
    schema: str,
    command: list[str],
    *,
    data: Any = None,
    ok: bool = True,
    actor: str = "actor_local",
    events: list[str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    refs: dict[str, Any] | None = None,
    start: float | None = None,
) -> dict[str, Any]:
    event_ids = events or []
    mode = response_mode(command)
    response_data = summary_data(schema, data) if mode == "summary" else data
    if mode == "summary":
        output = {
            "ok": ok,
            "schema": schema,
            "schema_version": SCHEMA_VERSION,
            "data": response_data if response_data is not None else {},
            "events": event_ids,
        }
        if diagnostics:
            output["diagnostics"] = diagnostics
        if warnings:
            output["warnings"] = warnings
        return output
    else:
        auto_refs = collect_refs({"data": response_data, "events": event_ids, "diagnostics": diagnostics or []})
        merged_refs = merge_refs(auto_refs, refs)
    output = {
        "ok": ok,
        "schema": schema,
        "schema_version": SCHEMA_VERSION,
        "command": summary_command(command) if mode == "summary" else command,
        "actor": actor,
        "data": response_data if response_data is not None else {},
        "events": event_ids,
        "diagnostics": diagnostics or [],
        "warnings": warnings or [],
        "cost": {
            "wall_ms": int((time.time() - start) * 1000) if start else 0,
            "input_bytes": 0,
            "output_bytes": 0,
            "external_cost": "0 USD",
        },
        "refs": merged_refs,
        "links": ref_links(merged_refs),
    }
    output["cost"]["output_bytes"] = len(response_json(output).encode())
    return output


def print_json(obj: dict[str, Any]) -> None:
    sys.stdout.write(response_json(obj))


def run_git(repo: Path, args: list[str], *, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise TriError("git_export_failed", stderr.strip() or "Git command failed.", retryable=False)
    return result.stdout


def run_git_worktree(git_dir: Path, worktree: Path, args: list[str], *, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", f"--work-tree={worktree}", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise TriError("git_export_failed", stderr.strip() or "Git command failed.", retryable=False)
    return result.stdout


def git_branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{branch}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0


def ensure_clean_git_repo(repo: Path) -> None:
    if not (repo / ".git").exists():
        raise TriError("path_not_found", "Git target repository not found.", object=str(repo))
    dirty = run_git(repo, ["status", "--short"], text=True).strip()
    if dirty:
        raise TriError("git_export_failed", "Target Git repository must be clean before export.", object=str(repo))


def git_default_ref(repo: Path) -> str:
    branch = run_git(repo, ["branch", "--show-current"], text=True).strip()
    if branch:
        return branch
    return "HEAD"


def actor_ref_from_git_author(name: str | None, email: str | None) -> str | None:
    seed = (email or name or "").strip().lower()
    if not seed:
        return None
    slug = "".join(ch if ch.isalnum() else "_" for ch in seed).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"human:{slug or sha256(seed.encode())[:12]}"


def git_author_metadata(repo: Path, ref: str) -> dict[str, Any]:
    raw = run_git(repo, ["show", "-s", "--format=%an%x00%ae%x00%aI", ref], text=True)
    parts = raw.rstrip("\n").split("\0")
    name = parts[0] if len(parts) > 0 and parts[0] else None
    email = parts[1] if len(parts) > 1 and parts[1] else None
    date = parts[2] if len(parts) > 2 and parts[2] else None
    return {"name": name, "email": email, "date": date, "actor": actor_ref_from_git_author(name, email)}


def git_cat_file_batch(repo: Path, object_ids: list[str]) -> dict[str, bytes]:
    if not object_ids:
        return {}
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(object_ids) + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise TriError("git_export_failed", stderr.strip() or "Git cat-file batch failed.", retryable=False)
    output = result.stdout
    offset = 0
    blobs: dict[str, bytes] = {}
    for expected_oid in object_ids:
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise TriError("git_export_failed", "Git cat-file batch returned a truncated header.", object=expected_oid)
        header = output[offset:header_end].decode(errors="replace")
        parts = header.split(" ")
        if len(parts) != 3:
            raise TriError("git_export_failed", "Git cat-file batch returned an invalid header.", object=expected_oid, header=header)
        oid, kind, size_text = parts
        try:
            size = int(size_text)
        except ValueError as exc:
            raise TriError("git_export_failed", "Git cat-file batch returned an invalid object size.", object=oid, size=size_text) from exc
        content_start = header_end + 1
        content_end = content_start + size
        if content_end > len(output):
            raise TriError("git_export_failed", "Git cat-file batch returned truncated object content.", object=oid)
        content = output[content_start:content_end]
        offset = content_end + 1
        if kind != "blob":
            raise TriError("git_export_failed", "Git cat-file batch returned a non-blob object.", object=oid, object_type=kind)
        blobs[oid] = content
        if oid != expected_oid:
            blobs[expected_oid] = content
    return blobs


def build_entries_from_git(store: Store, repo: Path, ref: str) -> dict[str, dict[str, Any]]:
    raw = run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", ref])
    assert isinstance(raw, bytes)
    records: list[tuple[str, str, str]] = []
    entries: dict[str, dict[str, Any]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, path_bytes = record.split(b"\t", 1)
        mode, kind, oid = header.decode().split(" ")
        if kind != "blob":
            continue
        path = normalize_path(path_bytes.decode())
        records.append((path, mode, oid))
    blobs = git_cat_file_batch(repo, [oid for _, _, oid in records])
    for path, mode, oid in records:
        content = blobs[oid]
        blob = store.put_blob(content)
        entry = {
            "blob": blob["id"],
            "hash": blob["hash"],
            "size": blob["size"],
            "mode": mode,
        }
        if mode == "120000":
            entry["symlink_target"] = content.decode(errors="replace")
        entries[path] = entry
    return dict(sorted(entries.items()))


def symlink_target_escapes(root: Path, link_path: Path, target: str) -> bool:
    target_path = Path(target)
    if target_path.is_absolute():
        return True
    root_resolved = root.resolve(strict=False)
    resolved_target = (link_path.parent / target).resolve(strict=False)
    try:
        return os.path.commonpath([str(root_resolved), str(resolved_target)]) != str(root_resolved)
    except ValueError:
        return True


def symlink_entry(store: Store, target: str) -> dict[str, Any]:
    data = target.encode()
    blob = store.put_blob(data)
    return {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "120000", "symlink_target": target}


def build_entries_from_dir(
    store: Store,
    root: Path,
    *,
    allow_symlinks: bool = False,
    reject_escaping_symlinks: bool = True,
) -> dict[str, dict[str, Any]]:
    if root.is_symlink():
        raise TriError("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(root))
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not allow_symlinks:
                raise TriError("path_escaped_mount", "Symlink materialization sync is not supported in the MVP.", object=str(path))
            rel = normalize_path(path.relative_to(root).as_posix())
            target = os.readlink(path)
            if reject_escaping_symlinks and symlink_target_escapes(root, path, target):
                raise TriError("path_escaped_mount", "Symlink target escapes the materialized workspace root.", object=str(path), target=target)
            entries[rel] = symlink_entry(store, target)
            continue
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = normalize_path(path.relative_to(root).as_posix())
        data = path.read_bytes()
        blob = store.put_blob(data)
        entries[rel] = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "100644"}
    return entries


def filter_ignored_entries(entries: dict[str, dict[str, Any]], ignored_patterns: list[str]) -> dict[str, dict[str, Any]]:
    if not ignored_patterns:
        return entries
    return {path: entry for path, entry in entries.items() if not path_matches_hidden(path, ignored_patterns)}


def make_materialization_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    try:
        root.chmod(0o700)
    except OSError:
        pass


def materialize_tree(store: Store, entries: dict[str, dict[str, Any]], root: Path) -> None:
    if root.is_symlink():
        raise TriError("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(root))
    if root.exists():
        make_materialization_writable(root)
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    for path, entry in entries.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("mode") == "120000":
            target = entry.get("symlink_target")
            if not isinstance(target, str):
                target = store.read_blob(entry["blob"]).decode(errors="replace")
            if symlink_target_escapes(root, dest, target):
                raise TriError("path_escaped_mount", "Stored symlink target escapes the materialized workspace root.", object=path, target=target)
            dest.symlink_to(target)
            continue
        dest.write_bytes(store.read_blob(entry["blob"]))


def tri_error_from_materialize(exc: MaterializeError) -> TriError:
    return TriError(exc.code, exc.message, **exc.fields)


def project_ignore_patterns(store: Store, project_id: str) -> list[str]:
    return [row["pattern"] for row in store.rows("SELECT pattern FROM project_ignores WHERE project_id = ? ORDER BY pattern", (project_id,))]


def import_rules(store: Store, project_id: str, entries: dict[str, dict[str, Any]]) -> IgnoreRules:
    """Rules deciding which *new* files a run, sync, or salvage may import:
    the snapshot's .gitignore files plus project-declared patterns."""

    def read_text(path: str) -> str | None:
        entry = entries.get(path)
        if not entry or entry.get("mode") == "120000":
            return None
        try:
            return store.read_blob(entry["blob"]).decode("utf-8", errors="replace")
        except TriError:
            return None

    junk = [".DS_Store", "Thumbs.db", "desktop.ini"]
    return IgnoreRules.from_entries(entries, read_text, [*junk, *project_ignore_patterns(store, project_id)])


def snapshot_base(store: Store, snapshot_id: str | None) -> tuple[str, dict[str, dict[str, Any]]] | None:
    """Base tree for whole-directory clones; only worth loading where dir clone exists."""
    if not snapshot_id or not clone_dir_supported():
        return None
    row = store.row("SELECT root_tree_id FROM snapshots WHERE id = ?", (snapshot_id,))
    if row is None:
        return None
    try:
        return row["root_tree_id"], store.load_tree(row["root_tree_id"])
    except TriError:
        return None


def materialize_entries(
    store: Store,
    entries: dict[str, dict[str, Any]],
    dest: Path,
    *,
    base_snapshot: str | None = None,
    attachments: list[Attachment] | tuple[Attachment, ...] = (),
    backend: str | None = None,
) -> Materialization:
    try:
        return store.materializer.materialize(entries, dest, base=snapshot_base(store, base_snapshot), attachments=attachments, backend=backend)
    except MaterializeError as exc:
        raise tri_error_from_materialize(exc) from None


def load_any_materialization(path: Path, baseline_file: Path) -> Materialization:
    payload = json.loads(Path(baseline_file).read_text())
    if payload.get("overlay"):
        return overlay_from_payload(path, payload)
    return load_materialization(path, baseline_file)


def writable_env_layer(env_root: Path) -> Path:
    """A writable-mode twin of a frozen env, built once per env, for use as an
    overlay lower layer: commands run as the caller there, so read-only modes
    would block the copy-up of any file they write. Uses clones where the volume
    supports them."""
    env_root = Path(env_root)
    twin = env_root.with_name(env_root.name + ".layer")
    if twin.is_dir():
        return twin
    staging = env_root.with_name(f".{env_root.name}.layer.{uuid.uuid4().hex}.tmp")
    method = probe_clone(env_root.parent)

    def copy_one(src: str, dst: str) -> None:
        if method is not None:
            try:
                clone_file(Path(src), Path(dst), method)
                shutil.copystat(src, dst)
                return
            except OSError:
                pass
        shutil.copy2(src, dst)

    shutil.copytree(env_root, staging, symlinks=True, copy_function=copy_one)
    make_tree_writable(staging)
    try:
        os.rename(staging, twin)
    except OSError:
        remove_tree(staging)
    return twin


def materialize_for_run(
    store: Store,
    entries: dict[str, dict[str, Any]],
    run_lease: "RunLease",
    *,
    base_snapshot: str | None,
    attachments: list[Attachment] | tuple[Attachment, ...] = (),
    allow_overlay: bool = True,
) -> Materialization:
    """Materialize an ephemeral run directory with the cheapest backend available:
    overlay (Linux namespaces) > clone > copy."""
    preference = (os.environ.get("TRI_MATERIALIZE_BACKEND") or "auto").lower()
    notes: list[dict[str, Any]] = []
    # Linux hosts that allow unprivileged user + mount namespaces get overlay runs
    # automatically (verified in CI); others fall back to clone (reflink) or copy.
    if allow_overlay and preference in ("auto", "overlay"):
        info = probe_overlay(store.runs)
        if info is not None:
            base_row = store.row("SELECT root_tree_id FROM snapshots WHERE id = ?", (base_snapshot,)) if base_snapshot else None
            base = (base_row["root_tree_id"], store.load_tree(base_row["root_tree_id"])) if base_row else None
            if base is None:
                base = (f"head_{entries_fingerprint(entries)[7:23]}", entries)
            layer_attachments = []
            for attachment in attachments:
                env_root = Path(attachment.source)
                for _ in attachment.rel.split("/"):
                    env_root = env_root.parent
                layer_attachments.append(Attachment(attachment.rel, writable_env_layer(env_root) / attachment.rel, attachment.mode))
            try:
                return prepare_overlay_run(
                    run_root=run_lease.dir,
                    bases=store.home / "bases",
                    cache=store.materializer.cache,
                    read_blob=store.read_blob,
                    entries=entries,
                    base=base,
                    attachments=layer_attachments,
                    info=info,
                )
            except MaterializeError as exc:
                if exc.code != "overlay_unsuitable":
                    raise tri_error_from_materialize(exc) from None
                notes.append({"code": "overlay_unsuitable", "message": exc.message})
                for leftover in ("upper", "ovl-work", "work", "deletions.txt"):
                    target = run_lease.dir / leftover
                    if target.is_dir():
                        remove_tree(target)
                    elif target.exists():
                        target.unlink()
        elif preference == "overlay":
            notes.append({"code": "overlay_unavailable", "message": "Overlay runs need Linux user+mount namespaces; used clone/copy instead."})
    materialization = materialize_entries(
        store,
        entries,
        run_lease.work,
        base_snapshot=base_snapshot,
        attachments=attachments,
        backend=None if preference in ("auto", "overlay") else preference,
    )
    materialization.notes.extend(notes)
    return materialization


def lease_sparse_paths(lease: sqlite3.Row) -> list[str]:
    return loads(row_value(lease, "sparse_paths_json"), []) or []


def sparse_entries(entries: dict[str, dict[str, Any]], patterns: list[str]) -> dict[str, dict[str, Any]]:
    """The subset of a tree a partial checkout lays out; no patterns means the whole tree."""
    if not patterns:
        return entries
    return {path: entry for path, entry in entries.items() if path_matches_filters(path, patterns)}


def entries_after_scan(
    store: Store,
    root: Path,
    before_entries: dict[str, dict[str, Any]],
    changed: list[str],
    deleted: list[str],
    observed: dict[str, list[int]] | None = None,
    *,
    skip_escaping: bool = False,
) -> dict[str, dict[str, Any]]:
    """Apply scanned changes to ``before_entries``; unchanged content keeps its entry.

    ``observed`` (when given) receives each changed path's stat key, taken
    before its content was read."""
    after = dict(before_entries)
    for path in deleted:
        after.pop(path, None)
    for raw_path in changed:
        path = normalize_path(raw_path)
        full = root / path
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            after.pop(path, None)
            continue
        if observed is not None:
            observed[path] = stat_key(st)
        prev = before_entries.get(path)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(full)
            if symlink_target_escapes(root, full, target):
                if skip_escaping:
                    continue
                raise TriError("path_escaped_mount", "Symlink target escapes the materialized workspace root.", object=str(full), target=target)
            entry = symlink_entry(store, target)
        elif stat.S_ISREG(st.st_mode):
            blob = store.put_blob(full.read_bytes())
            if os.name == "nt" and prev and prev.get("mode") in ("100644", "100755"):
                mode = prev["mode"]
            else:
                mode = "100755" if st.st_mode & 0o111 else "100644"
            entry = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": mode}
        else:
            continue
        if prev is not None and prev.get("blob") == entry["blob"] and prev.get("mode") == entry["mode"] and prev.get("symlink_target") == entry.get("symlink_target"):
            after[path] = prev
        else:
            after[path] = entry
    return dict(sorted(after.items()))


def make_materialization_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o555)
        else:
            path.chmod(0o444)
    root.chmod(0o555)


def event_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "actor": row["actor"],
        "project": row["project_id"],
        "workspace": row["workspace_id"],
        "task": row["task_id"],
        "kind": row["kind"],
        "object_refs": loads(row["object_refs_json"], {}),
        "input_fingerprints": loads(row["input_fingerprints_json"], {}),
        "output_fingerprints": loads(row["output_fingerprints_json"], {}),
        "policy_result": loads(row["policy_result_json"], {}),
        "created_at": row["created_at"],
    }


def ref_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        refs: list[str] = []
        for item in value:
            refs.extend(ref_strings(item))
        return refs
    if isinstance(value, dict):
        refs = []
        for item in value.values():
            refs.extend(ref_strings(item))
        return refs
    return []


def event_add_policy_candidate(store: Store, candidates: list[dict[str, Any]], seen: set[tuple[str, str]], object_id: Any, *, source: str) -> None:
    if not isinstance(object_id, str) or not object_id:
        return
    try:
        if object_id.startswith("review_"):
            review = store.get_review(object_id)
            policy_object = review["changeset_id"]
            policy_action = "changeset.read"
        elif object_id.startswith(("task_", "pkg_", "ws_", "cs_", "adv_", "rel_", "secret_")):
            ctx = object_policy_context(store, object_id)
            policy_object = object_id
            policy_action = default_grant_actions(ctx["type"])[0]
        else:
            return
    except TriError:
        return
    key = (policy_object, policy_action)
    if key in seen:
        return
    seen.add(key)
    candidates.append({"kind": "object", "object": policy_object, "action": policy_action, "source": source, "ref": object_id})


def event_add_artifact_candidate(candidates: list[dict[str, Any]], seen: set[tuple[str, str]], artifact_id: Any, *, source: str) -> None:
    if not isinstance(artifact_id, str) or not artifact_id.startswith("artifact_"):
        return
    key = (artifact_id, "artifact.read")
    if key in seen:
        return
    seen.add(key)
    candidates.append({"kind": "artifact", "object": artifact_id, "action": "artifact.read", "source": source, "ref": artifact_id})


def event_policy_candidates(store: Store, event: dict[str, Any]) -> list[dict[str, Any]]:
    refs = event["object_refs"]
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    workspace_scoped = bool(event.get("workspace"))

    event_add_policy_candidate(store, candidates, seen, event.get("workspace"), source="event.workspace")
    if not workspace_scoped:
        event_add_policy_candidate(store, candidates, seen, event.get("task"), source="event.task")
    for ref in ref_strings(refs):
        if workspace_scoped and ref.startswith("task_"):
            continue
        event_add_policy_candidate(store, candidates, seen, ref, source="event.object_refs")
        event_add_artifact_candidate(candidates, seen, ref, source="event.object_refs")

        if ref.startswith("check_"):
            try:
                check = check_data(store, ref)
            except TriError:
                continue
            if check["changeset"]:
                event_add_policy_candidate(store, candidates, seen, check["changeset"], source="event.check")
            for changeset_id in linked_changesets_for_check(store, ref):
                event_add_policy_candidate(store, candidates, seen, changeset_id, source="event.check_changeset")
            if check["workspace"]:
                event_add_policy_candidate(store, candidates, seen, check["workspace"], source="event.check")
        elif ref.startswith("conflict_"):
            row = store.row("SELECT workspace_id FROM workspace_conflicts WHERE id = ?", (ref,))
            if row is not None:
                event_add_policy_candidate(store, candidates, seen, row["workspace_id"], source="event.conflict")
        elif ref.startswith("export_"):
            row = store.row("SELECT changeset_id FROM exports WHERE id = ?", (ref,))
            if row is not None:
                event_add_policy_candidate(store, candidates, seen, row["changeset_id"], source="event.export")
        elif ref.startswith("lease_"):
            row = store.row("SELECT workspace_id FROM secret_leases WHERE id = ?", (ref,))
            if row is None:
                row = store.row("SELECT workspace_id FROM materialization_leases WHERE id = ?", (ref,))
            if row is not None:
                event_add_policy_candidate(store, candidates, seen, row["workspace_id"], source="event.lease")
        elif ref.startswith("op_"):
            row = store.row("SELECT workspace_id FROM operations WHERE id = ?", (ref,))
            if row is not None:
                event_add_policy_candidate(store, candidates, seen, row["workspace_id"], source="event.operation")
            for changeset_id in linked_changesets_for_operation(store, ref):
                event_add_policy_candidate(store, candidates, seen, changeset_id, source="event.operation_changeset")
        elif ref.startswith("run_"):
            row = store.row("SELECT task_id, workspace_id FROM task_runs WHERE id = ?", (ref,))
            if row is not None:
                event_add_policy_candidate(store, candidates, seen, row["workspace_id"], source="event.task_run")
                if not row["workspace_id"]:
                    event_add_policy_candidate(store, candidates, seen, row["task_id"], source="event.task_run")

    policy_object = event.get("policy_result", {}).get("object")
    event_add_policy_candidate(store, candidates, seen, policy_object, source="event.policy_result")
    return candidates


def event_access_decision(store: Store, event: dict[str, Any], *, actor: str) -> dict[str, Any]:
    selectors = actor_selectors(actor)
    candidates = event_policy_candidates(store, event)
    allowed = False
    reason = "missing_allow"
    grants: list[str] = []
    matched_candidates: list[dict[str, Any]] = []

    if actor == event["actor"]:
        allowed = True
        reason = "event_actor"
        grants.append("event_actor")
    if "role:maintainer" in selectors:
        allowed = True
        reason = "maintainer"
        grants.append("role:maintainer")

    if not allowed:
        for candidate in candidates:
            if candidate["kind"] == "artifact":
                decision, _ = artifact_access_decision(store, candidate["object"], actor=actor)
            else:
                decision, _ = object_access_decision(store, candidate["object"], actor=actor, action=candidate["action"])
            if decision["allowed"]:
                allowed = True
                reason = "origin_access"
                grants.extend(decision.get("grants", []))
                matched_candidates.append({**candidate, "decision_reason": decision.get("reason"), "matched_grants": decision.get("matched_grants", [])})
                break

    return {
        "id": new_id("decision"),
        "allowed": allowed,
        "actor": actor,
        "action": "event.read",
        "object": event["id"],
        "object_type": "event",
        "policies": ["local_policy_v0"],
        "actor_selectors": selectors,
        "policy_candidates": candidates,
        "matched_candidates": matched_candidates,
        "grants": sorted(set(grants)),
        "denials": [] if allowed else ["policy_denied"],
        "redactions": [],
        "reason_code": "allowed" if allowed else "policy_denied",
        "reason": reason if candidates or allowed else "unowned_event",
        "event": None,
    }


def event_hidden_patterns(store: Store, event: dict[str, Any], *, actor: str | None = None) -> list[str]:
    patterns: list[str] = []
    for candidate in event_policy_candidates(store, event):
        object_id = candidate["object"]
        try:
            if object_id.startswith("cs_"):
                changeset = store.get_changeset(object_id)
                patterns.extend(effective_hidden_patterns(store, changeset["project_id"], loads(changeset["hidden_paths_json"], []), actor=actor))
            elif object_id.startswith("ws_"):
                workspace = store.get_workspace(object_id)
                patterns.extend(effective_hidden_patterns(store, workspace["project_id"], loads(workspace["hidden_paths_json"], []), actor=actor))
        except TriError:
            continue
    if event.get("workspace"):
        try:
            workspace = store.get_workspace(event["workspace"])
            patterns.extend(effective_hidden_patterns(store, workspace["project_id"], loads(workspace["hidden_paths_json"], []), actor=actor))
        except TriError:
            pass
    if actor is not None and event.get("project"):
        patterns.extend(package_visibility_hidden_patterns(store, event["project"], actor))
    return sorted(set(patterns))


def redact_event_paths(value: Any, hidden_patterns: list[str], key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: redact_event_paths(item_value, hidden_patterns, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_event_paths(item, hidden_patterns, key) for item in value]
    if isinstance(value, str) and key in {"path", "paths", "src", "dest", "file_changes"}:
        visible, reason = path_visibility(value, hidden_patterns)
        if not visible:
            return {**redacted_path_ref(value), "redaction": reason}
    return value


def redacted_ref(object_type: str, action: str) -> dict[str, Any]:
    return {"redacted": True, "object_type": object_type, "required_grant": action}


def can_read_snapshot_ref(store: Store, snapshot: str, *, actor: str) -> bool | None:
    try:
        if snapshot_has_public_source(store, snapshot):
            return True
        candidates = snapshot_access_candidates(store, snapshot)
    except TriError:
        return None
    for object_id, action in candidates:
        decision, _ = object_access_decision(store, object_id, actor=actor, action=action)
        if decision["allowed"]:
            return True
    return False


def redact_sensitive_ref(store: Store, value: str, *, actor: str, counter: list[int]) -> Any:
    if value.startswith("lease_"):
        row = store.row("SELECT workspace_id FROM secret_leases WHERE id = ?", (value,))
        if row is None:
            row = store.row("SELECT workspace_id FROM materialization_leases WHERE id = ?", (value,))
        if row is None:
            return value
        decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
        if not decision["allowed"]:
            counter[0] += 1
            return redacted_ref("lease", "workspace.read")
        return value
    if value.startswith("run_"):
        row = store.row("SELECT task_id, workspace_id FROM task_runs WHERE id = ?", (value,))
        if row is None:
            return value
        if row["workspace_id"]:
            decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
            if not decision["allowed"]:
                counter[0] += 1
                return redacted_ref("task_run", "workspace.read")
        elif row["task_id"]:
            decision, _ = object_access_decision(store, row["task_id"], actor=actor, action="task.read")
            if not decision["allowed"]:
                counter[0] += 1
                return redacted_ref("task_run", "task.read")
        return value
    if value.startswith("artifact_"):
        try:
            decision, _ = artifact_access_decision(store, value, actor=actor)
        except TriError:
            return value
        if not decision["allowed"]:
            counter[0] += 1
            return redacted_ref("artifact", "artifact.read")
        return value
    if value.startswith("check_"):
        try:
            decision = check_access_decision(store, check_data(store, value), actor=actor)
        except TriError:
            return value
        if not decision["allowed"]:
            counter[0] += 1
            return redacted_ref("check", "check.read")
        return value
    if value.startswith("snap_"):
        allowed = can_read_snapshot_ref(store, value, actor=actor)
        if allowed is False:
            counter[0] += 1
            return redacted_ref("snapshot", "snapshot.read")
        return value
    if value.startswith(("task_", "pkg_", "ws_", "cs_", "review_", "adv_", "rel_", "secret_")):
        try:
            ctx = object_policy_context(store, value)
        except TriError:
            return value
        action = default_grant_actions(ctx["type"])[0]
        decision, _ = object_access_decision(store, value, actor=actor, action=action)
        if not decision["allowed"]:
            counter[0] += 1
            return redacted_ref(ctx["type"], action)
    return value


def redact_event_sensitive_refs(store: Store, value: Any, *, actor: str, counter: list[int]) -> Any:
    if isinstance(value, dict):
        return {item_key: redact_event_sensitive_refs(store, item_value, actor=actor, counter=counter) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_event_sensitive_refs(store, item, actor=actor, counter=counter) for item in value]
    if isinstance(value, str):
        return redact_sensitive_ref(store, value, actor=actor, counter=counter)
    return value


def redact_dependency_payload_refs(store: Store, value: Any, *, actor: str, counter: list[int]) -> Any:
    if isinstance(value, dict):
        result = {item_key: redact_dependency_payload_refs(store, item_value, actor=actor, counter=counter) for item_key, item_value in value.items()}
        parent = result.get("parent")
        if isinstance(parent, str) and parent.startswith("cs_"):
            try:
                decision, _ = object_access_decision(store, parent, actor=actor, action="changeset.read")
            except TriError:
                decision = {"allowed": True}
            if not decision["allowed"]:
                counter[0] += 1
                result["parent"] = None
                result["parent_redacted"] = True
                if "dependency_state" in result:
                    result["dependency_state"] = "redacted_parent"
                if "parent_status" in result:
                    result["parent_status"] = None
                if "parent_head_snapshot" in result:
                    result["parent_head_snapshot"] = None
        return result
    if isinstance(value, list):
        return [redact_dependency_payload_refs(store, item, actor=actor, counter=counter) for item in value]
    return value


def redact_policy_payload_refs(store: Store, value: Any, *, actor: str, counter: list[int]) -> Any:
    value = redact_dependency_payload_refs(store, value, actor=actor, counter=counter)
    return redact_event_sensitive_refs(store, value, actor=actor, counter=counter)


def event_view_data(store: Store, event: dict[str, Any], *, actor: str, record_denial: bool = True) -> dict[str, Any]:
    event = copy.deepcopy(event)
    decision = event_access_decision(store, event, actor=actor)
    if not decision["allowed"]:
        required_actions = sorted({candidate["action"] for candidate in decision["policy_candidates"]})
        fields: dict[str, Any] = {}
        if record_denial:
            evt = store.record_policy_decision(
                actor=actor,
                action="event.read",
                object_id=event["id"],
                decision=decision,
                project_id=event.get("project"),
                workspace_id=event.get("workspace"),
                task_id=event.get("task"),
                kind="policy.denied",
            )
            fields = {
                "decision": decision["id"],
                "event": evt,
                "explain_command": ["tri", "policy", "explain", decision["id"]],
            }
        raise TriError(
            "policy_denied",
            "Actor is not allowed to access this event.",
            object=event["id"],
            action="event.read",
            policy_candidate_count=len(decision["policy_candidates"]),
            redacted_candidates=len(decision["policy_candidates"]),
            required_grants=required_actions,
            **fields,
        )
    hidden_patterns = event_hidden_patterns(store, event, actor=actor)
    workspace_id, redacted_workspace = workspace_ref_for_actor(store, event.get("workspace"), actor=actor)
    event["workspace"] = workspace_id
    event["redacted_workspace"] = redacted_workspace
    event["object_refs"] = redact_event_paths(event["object_refs"], hidden_patterns)
    event["policy_result"] = redact_event_paths(event["policy_result"], hidden_patterns)
    redacted_refs = [0]
    event["object_refs"] = redact_event_sensitive_refs(store, event["object_refs"], actor=actor, counter=redacted_refs)
    event["input_fingerprints"] = redact_event_sensitive_refs(store, event["input_fingerprints"], actor=actor, counter=redacted_refs)
    event["output_fingerprints"] = redact_event_sensitive_refs(store, event["output_fingerprints"], actor=actor, counter=redacted_refs)
    event["policy_result"] = redact_policy_payload_refs(store, event["policy_result"], actor=actor, counter=redacted_refs)
    if redacted_refs[0]:
        event["redacted_object_refs"] = redacted_refs[0]
    return event


def event_view(store: Store, row: sqlite3.Row, *, actor: str, record_denial: bool = True) -> dict[str, Any]:
    return event_view_data(store, event_data(row), actor=actor, record_denial=record_denial)


def artifact_data(row: sqlite3.Row, *, include_text: bool = False) -> dict[str, Any]:
    path = Path(row["path"])
    raw = path.read_bytes()
    data = {
        "id": row["id"],
        "kind": row["kind"],
        "path": None,
        "storage": {
            "backend": "tri_local_artifact_store",
            "path_redacted": True,
            "content_command": ["artifacts", "read", row["id"]],
            "content_route": f"/v0/artifacts/{row['id']}/content",
        },
        "size": row["size"],
        "fingerprint": f"sha256:{sha256(raw)}",
        "labels": loads(row["labels_json"], []),
        "created_at": row["created_at"],
    }
    if include_text:
        try:
            data["encoding"] = "utf-8"
            data["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            data["encoding"] = "base64"
            data["text"] = base64.b64encode(raw).decode()
    return data


def secret_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": row["project_id"],
        "name": row["name"],
        "provider": row["provider"],
        "fingerprint": row["fingerprint"],
        "scope": loads(row["scope_json"], []),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


def secret_lease_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "secret": row["secret_id"],
        "workspace": row["workspace_id"],
        "actor": row["actor"],
        "env": row["env_name"],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


def materialization_lease_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace": row["workspace_id"],
        "path": row["path"],
        "owner": row["actor"],
        "status": row["status"],
        "read_only": row["status"] == "read_only",
        "mounted_snapshot": row["mounted_snapshot_id"],
        "last_sync_snapshot": row["last_sync_snapshot_id"],
        "ignored_paths": loads(row["ignored_paths_json"], []),
        "sparse_paths": lease_sparse_paths(row),
        "generated_artifacts": loads(row["generated_artifacts_json"], []),
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
        "backend": row_value(row, "backend"),
        "degraded": None if row_value(row, "degraded") is None else bool(row_value(row, "degraded")),
        "ttl_seconds": row_value(row, "ttl_seconds"),
        "grandfathered": bool(row_value(row, "grandfathered", 0)),
        "attention": loads(row_value(row, "attention_json"), None) if row_value(row, "attention_json") else None,
        "reaped_at": row_value(row, "reaped_at"),
        "salvage": row_value(row, "salvage_id"),
    }


def row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def resolve_lease_for_inspect(store: Store, lease_id: str) -> tuple[str, sqlite3.Row]:
    materialization = store.row("SELECT * FROM materialization_leases WHERE id = ?", (lease_id,))
    if materialization is not None:
        return "materialization", materialization
    secret = store.row("SELECT * FROM secret_leases WHERE id = ?", (lease_id,))
    if secret is not None:
        return "secret", secret
    disk = store.row("SELECT * FROM disk_leases WHERE id = ?", (lease_id,))
    if disk is not None:
        return "directory", disk
    raise TriError("path_not_found", "Lease not found.", object=lease_id)


def secret_lease_inspect_access(store: Store, lease: sqlite3.Row, *, actor: str) -> None:
    selectors = actor_selectors(actor)
    if lease["actor"] == actor or "role:maintainer" in selectors:
        return
    decision, _ = object_access_decision(store, lease["workspace_id"], actor=actor, action="workspace.write")
    if decision["allowed"]:
        return
    raise TriError(
        "policy_denied",
        "Actor is not allowed to inspect this secret lease.",
        object=lease["id"],
        action="lease.read",
        lease_actor=lease["actor"],
        required_grant="lease_actor or workspace.write",
    )


def lease_inspect_data(kind: str, lease: dict[str, Any]) -> dict[str, Any]:
    schema = "tri.secret_lease.inspect" if kind == "secret" else "tri.workspace.materialization_lease"
    return {"id": lease["id"], "kind": kind, "schema": schema, "lease": lease}


def materialization_lease_expired(row: sqlite3.Row) -> bool:
    return bool(row["expires_at"] and row["expires_at"] <= utcnow())


def implicit_materialization_lease(store: Store, workspace_id: str, *, actor: str | None = None) -> sqlite3.Row | None:
    rows = store.rows(
        "SELECT * FROM materialization_leases WHERE workspace_id = ? AND status = ? ORDER BY created_at DESC, rowid DESC",
        (workspace_id, "active"),
    )
    if actor is not None:
        row = store.row(
            "SELECT * FROM materialization_leases WHERE workspace_id = ? AND actor = ? AND status = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (workspace_id, actor, "active"),
        )
        if row is not None:
            return row
        if len(rows) <= 1:
            return None
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    raise TriError(
        "stale_materialization",
        "Workspace has multiple active materialization leases; pass --lease to choose the sync authority.",
        object=workspace_id,
        actor=actor,
        leases=[row["id"] for row in rows],
    )


def resolve_materialization_lease(store: Store, workspace_id: str, lease_id: str | None = None, *, actor: str | None = None) -> sqlite3.Row:
    if lease_id:
        lease = store.get_materialization_lease(lease_id)
        if lease["workspace_id"] != workspace_id:
            raise TriError("stale_materialization", "Materialization lease is not scoped to this workspace.", object=lease_id, workspace=workspace_id)
        return lease
    row = implicit_materialization_lease(store, workspace_id, actor=actor)
    if row is None:
        raise TriError("stale_materialization", "Workspace has no active materialization lease.", object=workspace_id)
    return row


def require_active_materialization_lease(store: Store, lease: sqlite3.Row) -> None:
    if lease["status"] != "active":
        raise TriError("stale_materialization", "Materialization lease is not active.", object=lease["id"], status=lease["status"])
    if materialization_lease_expired(lease):
        with store.conn:
            store.conn.execute("UPDATE materialization_leases SET status = ? WHERE id = ? AND status = ?", ("expired", lease["id"], "active"))
        raise TriError("stale_materialization", "Materialization lease has expired.", object=lease["id"], expired_at=lease["expires_at"])


def read_secret_value(store: Store, secret: sqlite3.Row) -> bytes:
    if secret["revoked_at"]:
        raise TriError("policy_denied", "Secret has been revoked.", object=secret["id"])
    return Path(secret["value_path"]).read_bytes()


def generated_config_path(profile: str) -> str:
    profile = profile.strip()
    if not profile:
        raise TriError("invalid_command", "Config profile cannot be empty.")
    return normalize_path(f".tri/generated-config/{profile}.env")


def write_materialized_config(root: Path, relpath: str, content: bytes) -> Path:
    if root.is_symlink():
        raise TriError("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(root))
    root_resolved = root.resolve(strict=True)
    dest = root / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve(strict=False)
    try:
        if os.path.commonpath([str(root_resolved), str(dest_resolved)]) != str(root_resolved):
            raise TriError("path_escaped_mount", "Generated config path escapes the materialized workspace.", object=str(dest), root=str(root_resolved))
    except ValueError:
        raise TriError("path_escaped_mount", "Generated config path escapes the materialized workspace.", object=str(dest), root=str(root_resolved))
    dest.write_bytes(content)
    return dest_resolved


def resolve_secret_for_workspace(store: Store, workspace: sqlite3.Row, name_or_id: str) -> sqlite3.Row:
    row = store.row(
        "SELECT * FROM secret_refs WHERE project_id = ? AND (id = ? OR name = ?)",
        (workspace["project_id"], name_or_id, name_or_id),
    )
    if row is None:
        raise TriError("path_not_found", "Secret not found for workspace project.", object=name_or_id)
    return row


def active_secret_leases(store: Store, workspace_id: str, lease_ids: list[str], *, actor: str) -> tuple[list[dict[str, Any]], dict[str, str], list[tuple[str, bytes]]]:
    leases: list[dict[str, Any]] = []
    env: dict[str, str] = {}
    redactions: list[tuple[str, bytes]] = []
    now = utcnow()
    selectors = actor_selectors(actor)
    for lease_id in lease_ids:
        lease = store.get_secret_lease(lease_id)
        if lease["workspace_id"] != workspace_id:
            raise TriError("policy_denied", "Secret lease is not scoped to this workspace.", object=lease_id)
        if lease["actor"] != actor and "role:maintainer" not in selectors:
            raise TriError(
                "policy_denied",
                "Secret lease is bound to the actor that requested it.",
                object=lease_id,
                lease_actor=lease["actor"],
                actor=actor,
                required_grant="lease_actor",
            )
        if lease["status"] != "active":
            raise TriError("policy_denied", "Secret lease is not active.", object=lease_id, status=lease["status"])
        if lease["expires_at"] and lease["expires_at"] <= now:
            raise TriError("policy_denied", "Secret lease has expired.", object=lease_id, expired_at=lease["expires_at"])
        secret = store.get_secret(lease["secret_id"])
        value = read_secret_value(store, secret)
        try:
            env[lease["env_name"]] = value.decode()
        except UnicodeDecodeError:
            env[lease["env_name"]] = base64.b64encode(value).decode()
        leases.append(secret_lease_data(lease))
        if value:
            redactions.append((lease["id"], value))
    return leases, env, redactions


def redact_secret_bytes(data: bytes, redactions: list[tuple[str, bytes]]) -> bytes:
    redacted = data
    for lease_id, value in redactions:
        if value:
            redacted = redacted.replace(value, f"[tri-secret-redacted:{lease_id}]".encode())
    return redacted


def local_check_cost(command: list[str], duration_ms: int, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    return {
        "wall_ms": duration_ms,
        "input_bytes": len(dumps(command).encode()),
        "output_bytes": len(stdout) + len(stderr),
        "external_cost": "0 USD",
    }


def entries_fingerprint(entries: dict[str, dict[str, Any]]) -> str:
    return f"sha256:{sha256(dumps(entries).encode())}"


def materialized_directory_record(
    *,
    path: Path,
    input_snapshot: str,
    output_snapshot: str,
    before_entries: dict[str, dict[str, Any]],
    after_entries: dict[str, dict[str, Any]],
    mode: str,
    materialization: Materialization | None = None,
) -> dict[str, Any]:
    record = {
        "path": str(path),
        "mode": mode,
        "input_snapshot": input_snapshot,
        "output_snapshot": output_snapshot,
        "fingerprint_before": entries_fingerprint(before_entries),
        "fingerprint_after": entries_fingerprint(after_entries),
        "mutated": changed_paths(before_entries, after_entries) != [],
    }
    if materialization is not None:
        record.update(materialization.describe())
    return record


def check_environment_policy(secret_leases: list[dict[str, Any]], secret_env: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "mode": "process_env_plus_scoped_secret_leases",
        "inherits_process_env": True,
        "secret_leases": [lease["id"] for lease in secret_leases],
        "secret_env": sorted((secret_env or {}).keys()),
        "redaction": "lease_values_redacted_before_log_storage",
    }


def generated_config_records_for_run(store: Store, workspace_id: str, *, actor: str) -> list[dict[str, Any]]:
    records_by_path: dict[str, dict[str, Any]] = {}
    for row in store.rows(
        "SELECT * FROM events WHERE kind = ? AND workspace_id = ? ORDER BY created_at, rowid",
        ("config.materialized", workspace_id),
    ):
        event = event_data(row)
        if event["actor"] != actor:
            continue
        refs = event["object_refs"]
        artifact_id = refs.get("artifact")
        if not isinstance(artifact_id, str) or not artifact_id.startswith("artifact_"):
            continue
        profile = refs.get("profile")
        path = refs.get("path") if isinstance(refs.get("path"), str) else generated_config_path(str(profile or "default"))
        lease_id = refs.get("lease")
        if lease_id:
            try:
                lease = store.get_materialization_lease(lease_id)
            except TriError:
                continue
            if lease["workspace_id"] != workspace_id or lease["actor"] != actor or lease["status"] != "active" or materialization_lease_expired(lease):
                continue
        artifact = store.get_artifact(artifact_id)
        labels = loads(artifact["labels_json"], [])
        if "non-exportable" not in labels:
            continue
        records_by_path[path] = {
            "artifact": artifact_id,
            "path": path,
            "profile": profile,
            "lease": lease_id,
            "labels": labels,
        }
    return [records_by_path[path] for path in sorted(records_by_path)]


def materialize_generated_configs_for_run(store: Store, root: Path, configs: list[dict[str, Any]]) -> None:
    for config in configs:
        artifact = store.get_artifact(config["artifact"])
        write_materialized_config(root, config["path"], Path(artifact["path"]).read_bytes())


def filter_generated_config_entries(
    before_entries: dict[str, dict[str, Any]],
    after_entries: dict[str, dict[str, Any]],
    configs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not configs:
        return after_entries
    reserved_patterns = sorted({config["path"] for config in configs} | {".tri/generated-config/**"})
    return {path: entry for path, entry in after_entries.items() if path in before_entries or not path_matches_hidden(path, reserved_patterns)}


def secret_redactions_for_leases(store: Store, lease_ids: list[str], *, workspace_id: str | None = None) -> list[tuple[str, bytes]]:
    redactions: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for lease_id in lease_ids:
        if lease_id in seen:
            continue
        seen.add(lease_id)
        lease = store.get_secret_lease(lease_id)
        if workspace_id and lease["workspace_id"] != workspace_id:
            raise TriError("policy_denied", "Secret lease does not belong to this check workspace.", object=lease_id, workspace=workspace_id)
        secret = store.get_secret(lease["secret_id"])
        value_path = Path(secret["value_path"])
        if not value_path.exists():
            raise TriError("path_not_found", "Secret value is not available for log redaction.", object=secret["id"], lease=lease_id)
        redactions.append((lease_id, value_path.read_bytes()))
    return redactions


def changed_paths(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> list[str]:
    paths = sorted(set(old) | set(new))
    return [path for path in paths if old.get(path) != new.get(path)]


def blob_content_fingerprint(store: Store, blob_id: str | None) -> str | None:
    if blob_id is None:
        return None
    row = store.row("SELECT hash FROM blobs WHERE id = ?", (blob_id,))
    if row is None:
        raise TriError("path_not_found", "Blob not found.", object=blob_id)
    return f"sha256:{row['hash']}"


def blob_is_binary(store: Store, blob_id: str | None) -> bool:
    if blob_id is None:
        return False
    return b"\0" in store.read_blob(blob_id)


def blob_row(store: Store, blob_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM blobs WHERE id = ?", (blob_id,))
    if row is None:
        raise TriError("path_not_found", "Blob not found.", object=blob_id)
    return row


def tree_row(store: Store, tree_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM trees WHERE id = ?", (tree_id,))
    if row is None:
        raise TriError("path_not_found", "Tree not found.", object=tree_id)
    return row


def patch_for_path(store: Store, path: str, old_entry: dict[str, Any] | None, new_entry: dict[str, Any] | None) -> str | None:
    old_bytes = store.read_blob(old_entry["blob"]) if old_entry else b""
    new_bytes = store.read_blob(new_entry["blob"]) if new_entry else b""
    if b"\0" in old_bytes or b"\0" in new_bytes:
        return None
    old_lines = old_bytes.decode(errors="replace").splitlines(keepends=True)
    new_lines = new_bytes.decode(errors="replace").splitlines(keepends=True)
    chunks = [f"diff --git a/{path} b/{path}\n"]
    if old_entry is None:
        chunks.append(f"new file mode {new_entry.get('mode', '100644') if new_entry else '100644'}\n")
        old_label = "/dev/null"
    else:
        old_label = f"a/{path}"
    if new_entry is None:
        chunks.append(f"deleted file mode {old_entry.get('mode', '100644') if old_entry else '100644'}\n")
        new_label = "/dev/null"
    else:
        new_label = f"b/{path}"
    chunks.extend(difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label))
    if chunks and not chunks[-1].endswith("\n"):
        chunks[-1] += "\n"
    return "".join(chunks)


def diff_data(
    store: Store,
    base_snapshot: str,
    head_snapshot: str,
    hidden_patterns: list[str] | None = None,
    *,
    path_patterns: list[str] | None = None,
    mode: str = "machine",
) -> dict[str, Any]:
    base = store.load_snapshot_tree(base_snapshot)
    head = store.load_snapshot_tree(head_snapshot)
    filters = normalize_path_patterns(path_patterns)
    files = []
    summary = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0, "binary": 0, "redacted": 0, "policy_denied": 0}
    for path in sorted(set(base) | set(head)):
        if not path_matches_filters(path, filters):
            continue
        old = base.get(path)
        new = head.get(path)
        if old == new:
            continue
        if old is None:
            status = "added"
        elif new is None:
            status = "deleted"
        else:
            status = "modified"
        summary[status] += 1
        visible, reason = path_visibility(path, hidden_patterns)
        if not visible:
            summary["redacted"] += 1
            if reason == "hidden_path":
                summary["policy_denied"] += 1
            files.append(
                {
                    **redacted_path_ref(path),
                    "status": status,
                    "old_blob": None,
                    "new_blob": None,
                    "visible": False,
                    "redaction": reason,
                }
            )
            continue
        old_blob = old["blob"] if old else None
        new_blob = new["blob"] if new else None
        binary = blob_is_binary(store, old_blob) or blob_is_binary(store, new_blob)
        if binary:
            summary["binary"] += 1
        file_data = {
            "path": path,
            "status": status,
            "old_blob": old_blob,
            "new_blob": new_blob,
            "old_content_fingerprint": blob_content_fingerprint(store, old_blob),
            "new_content_fingerprint": blob_content_fingerprint(store, new_blob),
            "binary": binary,
            "visible": True,
        }
        if mode == "patch":
            patch = patch_for_path(store, path, old, new)
            file_data["patch"] = patch
            file_data["patch_unavailable"] = "binary" if patch is None and binary else None
        files.append(file_data)
    if mode == "summary":
        files = []
    if mode == "patch":
        patch = "".join(file.get("patch") or "" for file in files if file.get("visible"))
    else:
        patch = None
    return {"base_snapshot": base_snapshot, "head_snapshot": head_snapshot, "mode": mode, "path_filters": filters, "summary": summary, "files": files, "patch": patch}


def clear_git_export_worktree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)


def materialize_git_export_tree(store: Store, entries: dict[str, dict[str, Any]], root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    symlink_paths = {path for path, entry in entries.items() if entry.get("mode") == "120000"}
    for path, entry in entries.items():
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in symlink_paths:
                raise TriError("git_export_failed", "Tree contains a path below a symlink.", object=path, parent=parent, retryable=False)
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if entry.get("mode") == "120000":
            target = entry.get("symlink_target")
            if not isinstance(target, str):
                target = store.read_blob(entry["blob"]).decode(errors="replace")
            dest.symlink_to(target)
            continue
        dest.write_bytes(store.read_blob(entry["blob"]))
        if entry.get("mode") == "100755":
            dest.chmod(0o755)
        else:
            dest.chmod(0o644)


def git_binary_patch_for_entries(store: Store, base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]]) -> str:
    with tempfile.TemporaryDirectory(prefix="tri-git-patch-") as tmp:
        tmp_root = Path(tmp)
        git_dir = tmp_root / "git"
        worktree = tmp_root / "worktree"
        git_dir.mkdir()
        worktree.mkdir()
        run_git_worktree(git_dir, worktree, ["init"], text=True)
        run_git_worktree(git_dir, worktree, ["config", "user.email", "tri@example.invalid"], text=True)
        run_git_worktree(git_dir, worktree, ["config", "user.name", "Tri Export"], text=True)
        run_git_worktree(git_dir, worktree, ["config", "core.autocrlf", "false"], text=True)
        materialize_git_export_tree(store, base, worktree)
        run_git_worktree(git_dir, worktree, ["add", "-A"], text=True)
        run_git_worktree(git_dir, worktree, ["commit", "--allow-empty", "-m", "tri base"], text=True)
        clear_git_export_worktree(worktree)
        materialize_git_export_tree(store, head, worktree)
        run_git_worktree(git_dir, worktree, ["add", "-A"], text=True)
        patch = run_git_worktree(git_dir, worktree, ["diff", "--binary", "--cached", "HEAD"], text=True)
        assert isinstance(patch, str)
        return patch


def patch_for_snapshots(store: Store, base_snapshot: str, head_snapshot: str, hidden_patterns: list[str] | None = None) -> str:
    base = store.load_snapshot_tree(base_snapshot)
    head = store.load_snapshot_tree(head_snapshot)
    needs_binary_patch = False
    for path in sorted(set(base) | set(head)):
        old_entry = base.get(path)
        new_entry = head.get(path)
        if old_entry == new_entry:
            continue
        visible, reason = path_visibility(path, hidden_patterns)
        if reason == "secret_like_path":
            raise TriError(
                "secret_export_blocked",
                "Refusing to export secret-like path.",
                object=path,
                retryable=False,
            )
        if not visible:
            raise TriError(
                "visibility_export_blocked",
                "Refusing to export a policy-hidden path.",
                path_hash=redacted_path_ref(path)["path_hash"],
                retryable=False,
            )
        if blob_is_binary(store, old_entry["blob"] if old_entry else None) or blob_is_binary(store, new_entry["blob"] if new_entry else None):
            needs_binary_patch = True
    if needs_binary_patch:
        return git_binary_patch_for_entries(store, base, head)
    return diff_data(store, base_snapshot, head_snapshot, hidden_patterns, mode="patch")["patch"] or ""


def raise_stale_workspace_head(store: Store, workspace_id: str, expected: str, message: str) -> None:
    row = store.row("SELECT head_snapshot_id FROM workspaces WHERE id = ?", (workspace_id,))
    actual = row["head_snapshot_id"] if row is not None else None
    raise TriError(
        "stale_workspace_head",
        message,
        object=workspace_id,
        expected=expected,
        actual=actual,
        retryable=True,
    )


def mutate_workspace(
    store: Store,
    workspace_id: str,
    *,
    actor: str,
    kind: str,
    apply_change: Any,
    metadata: dict[str, Any] | None = None,
    expect_head: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    workspace = store.get_workspace(workspace_id)
    if workspace["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can mutate.", object=workspace_id)
    policy_decision = require_object_access(store, workspace_id, actor=actor, action="workspace.write")
    input_snapshot = workspace["head_snapshot_id"]
    if expect_head and expect_head != input_snapshot:
        raise TriError(
            "stale_workspace_head",
            "Workspace head changed before mutation could commit.",
            object=workspace_id,
            expected=expect_head,
            actual=input_snapshot,
            retryable=True,
        )
    entries = store.load_snapshot_tree(input_snapshot)
    new_entries, paths = apply_change(dict(entries))
    with store.conn:
        snapshot = store.create_snapshot(
            workspace["project_id"],
            new_entries,
            parents=[input_snapshot],
            source="workspace",
            actor=actor,
            metadata={"workspace": workspace_id, "operation": kind},
        )
        op_id = new_id("op")
        store.conn.execute(
            """
            INSERT INTO operations (
              id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id,
              path_set_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                op_id,
                workspace_id,
                actor,
                kind,
                input_snapshot,
                snapshot["id"],
                dumps(paths),
                dumps(metadata or {}),
                utcnow(),
            ),
        )
        updated = store.conn.execute(
            """
            UPDATE workspaces
            SET head_snapshot_id = ?, updated_at = ?
            WHERE id = ? AND head_snapshot_id = ?
            """,
            (snapshot["id"], utcnow(), workspace_id, input_snapshot),
        )
        if updated.rowcount != 1:
            raise_stale_workspace_head(store, workspace_id, input_snapshot, "Workspace head changed before mutation could commit.")
        policy_event = new_id("evt")
        policy_decision["event"] = policy_event
        store.create_event(
            kind="policy.evaluated",
            actor=actor,
            event_id=policy_event,
            project_id=workspace["project_id"],
            workspace_id=workspace_id,
            task_id=workspace["task_id"],
            object_refs={"decision": policy_decision["id"], "action": "workspace.write", "object": workspace_id},
            policy_result=policy_decision,
        )
        policy_provenance = policy_provenance_data(policy_decision)
        stored_metadata = {**(metadata or {}), "policy": policy_provenance}
        store.conn.execute("UPDATE operations SET metadata_json = ? WHERE id = ?", (dumps(stored_metadata), op_id))
        phase_evt = set_workspace_task_run_phase(store, workspace, actor=actor, status="mutating", reason=kind)
        evt = store.create_event(
            kind=f"workspace.{kind}",
            actor=actor,
            project_id=workspace["project_id"],
            workspace_id=workspace_id,
            task_id=workspace["task_id"],
            object_refs={"operation": op_id, "snapshot": snapshot["id"], "paths": paths},
            input_fingerprints={"snapshot": input_snapshot},
            output_fingerprints={"snapshot": snapshot["id"]},
            policy_result=policy_decision,
        )
    events = [policy_event, evt] if phase_evt is None else [phase_evt, policy_event, evt]
    return {"operation": op_id, "snapshot": snapshot["id"], "paths": paths}, events


def import_source_data(metadata: dict[str, Any], snapshot: dict[str, str]) -> dict[str, Any]:
    return {
        "source_path": metadata.get("source_path"),
        "import_ref": metadata.get("import_ref"),
        "git_commit": metadata.get("git_commit"),
        "git_tree": metadata.get("git_tree"),
        "git_author": metadata.get("git_author"),
        "tri_snapshot": snapshot["id"],
        "tri_root_tree": snapshot["root_tree"],
        "tri_content_fingerprint": snapshot["content_fingerprint"],
    }


def cmd_import(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    require_not_task_scoped_actor(args.actor, action="project.import", object=str(repo))
    if not repo.exists():
        raise TriError("path_not_found", "Repository path not found.", object=str(repo))
    ref = args.ref or (git_default_ref(repo) if (repo / ".git").exists() else "working")
    target_name = args.target or (ref if ref != "HEAD" else "head")
    entries = build_entries_from_git(store, repo, ref) if (repo / ".git").exists() else build_entries_from_dir(store, repo, allow_symlinks=True, reject_escaping_symlinks=False)
    now = utcnow()
    project_id = new_id("proj")
    with store.conn:
        store.conn.execute(
            "INSERT INTO projects (id, name, root_path, created_at) VALUES (?, ?, ?, ?)",
            (project_id, args.name, str(repo), now),
        )
        metadata: dict[str, Any] = {"source_path": str(repo), "import_ref": ref}
        if (repo / ".git").exists():
            metadata["git_commit"] = run_git(repo, ["rev-parse", ref], text=True).strip()
            metadata["git_tree"] = run_git(repo, ["rev-parse", f"{ref}^{{tree}}"], text=True).strip()
            metadata["git_author"] = git_author_metadata(repo, ref)
        snapshot = store.create_snapshot(project_id, entries, parents=[], source="imported", actor=args.actor, metadata=metadata)
        target_id = new_id("target")
        store.conn.execute(
            "INSERT INTO targets (id, project_id, name, snapshot_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (target_id, project_id, target_name, snapshot["id"], now),
        )
        evt = store.create_event(
            kind="project.imported",
            actor=args.actor,
            project_id=project_id,
            object_refs={"project": project_id, "target": target_id, "snapshot": snapshot["id"]},
            output_fingerprints={"snapshot": snapshot["content_fingerprint"]},
        )
    return envelope(
        "tri.import",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={
            "project": {"id": project_id, "name": args.name},
            "target": {"id": target_id, "name": target_name, "snapshot": snapshot["id"]},
            "snapshot": snapshot,
            "source": import_source_data(metadata, snapshot),
            "files": len(entries),
        },
    )


def project_targets_data(store: Store, project_id: str) -> list[dict[str, Any]]:
    return [
        {"id": row["id"], "name": row["name"], "snapshot": row["snapshot_id"], "created_at": row["created_at"]}
        for row in store.rows("SELECT * FROM targets WHERE project_id = ? ORDER BY name", (project_id,))
    ]


def resolve_target_for_project(store: Store, project_id: str, target_ref: str) -> sqlite3.Row:
    row = store.row(
        """
        SELECT * FROM targets
        WHERE project_id = ? AND (id = ? OR name = ?)
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (project_id, target_ref, target_ref, target_ref),
    )
    if row is None:
        raise TriError("path_not_found", "Target not found.", object=target_ref)
    return row


def resolve_target_for_inspect(store: Store, project_or_target: str, target_ref: str | None = None) -> sqlite3.Row:
    if target_ref is not None:
        project = store.get_project(project_or_target)
        return resolve_target_for_project(store, project["id"], target_ref)
    if not project_or_target.startswith("target_"):
        raise TriError(
            "invalid_command",
            "Inspecting a target by name requires a project.",
            object=project_or_target,
            usage="tri targets inspect <project> <target-or-id>",
        )
    row = store.row("SELECT * FROM targets WHERE id = ?", (project_or_target,))
    if row is None:
        raise TriError("path_not_found", "Target not found.", object=project_or_target)
    return row


def target_data(store: Store, row: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": row["project_id"],
        "name": row["name"],
        "snapshot": row["snapshot_id"],
        "snapshot_data": snapshot_data(store, row["snapshot_id"], actor=actor),
        "created_at": row["created_at"],
    }


def count_row(store: Store, sql: str, params: tuple[Any, ...]) -> int:
    return int(store.row(sql, params)["count"])


def project_counts(store: Store, project_id: str) -> dict[str, int]:
    return {
        "targets": count_row(store, "SELECT COUNT(*) AS count FROM targets WHERE project_id = ?", (project_id,)),
        "package_boundaries": count_row(store, "SELECT COUNT(*) AS count FROM package_boundaries WHERE project_id = ?", (project_id,)),
        "snapshots": count_row(store, "SELECT COUNT(*) AS count FROM snapshots WHERE project_id = ?", (project_id,)),
        "workspaces": count_row(store, "SELECT COUNT(*) AS count FROM workspaces WHERE project_id = ?", (project_id,)),
        "tasks": count_row(store, "SELECT COUNT(*) AS count FROM tasks WHERE project_id = ?", (project_id,)),
        "task_runs": count_row(
            store,
            """
            SELECT COUNT(*) AS count FROM task_runs
            LEFT JOIN tasks ON tasks.id = task_runs.task_id
            LEFT JOIN workspaces ON workspaces.id = task_runs.workspace_id
            WHERE tasks.project_id = ? OR workspaces.project_id = ?
            """,
            (project_id, project_id),
        ),
        "check_sets": count_row(store, "SELECT COUNT(*) AS count FROM check_sets WHERE project_id = ?", (project_id,)),
        "checks": count_row(
            store,
            """
            SELECT COUNT(DISTINCT check_runs.id) AS count FROM check_runs
            LEFT JOIN workspaces ON workspaces.id = check_runs.workspace_id
            LEFT JOIN changesets ON changesets.id = check_runs.changeset_id
            WHERE workspaces.project_id = ? OR changesets.project_id = ?
            """,
            (project_id, project_id),
        ),
        "changesets": count_row(store, "SELECT COUNT(*) AS count FROM changesets WHERE project_id = ?", (project_id,)),
        "reviews": count_row(store, "SELECT COUNT(*) AS count FROM reviews JOIN changesets ON changesets.id = reviews.changeset_id WHERE changesets.project_id = ?", (project_id,)),
        "exports": count_row(store, "SELECT COUNT(*) AS count FROM exports JOIN changesets ON changesets.id = exports.changeset_id WHERE changesets.project_id = ?", (project_id,)),
        "events": count_row(store, "SELECT COUNT(*) AS count FROM events WHERE project_id = ?", (project_id,)),
        "secrets": count_row(store, "SELECT COUNT(*) AS count FROM secret_refs WHERE project_id = ?", (project_id,)),
        "advisories": count_row(store, "SELECT COUNT(*) AS count FROM advisories WHERE project_id = ?", (project_id,)),
        "releases": count_row(store, "SELECT COUNT(*) AS count FROM releases WHERE project_id = ?", (project_id,)),
    }


def project_data(store: Store, row: sqlite3.Row, *, actor: str | None = None) -> dict[str, Any]:
    targets = project_targets_data(store, row["id"])
    package_boundaries, redacted_package_boundaries = package_boundaries_data(store, row["id"], actor=actor)
    return {
        "id": row["id"],
        "name": row["name"],
        "root_path": row["root_path"],
        "default_target": targets[0]["name"] if targets else None,
        "targets": targets,
        "package_boundaries": package_boundaries,
        "redacted_package_boundaries": redacted_package_boundaries,
        "visibility_defaults": {"workspace": ["private"], "task": ["public"], "advisory": ["embargoed"]},
        "policy_set": {"engine": "local_policy_v0"},
        "import_settings": {"source_path": row["root_path"]},
        "export_settings": {"adapters": ["git_patch", "git_commit", "github_pr"]},
        "counts": project_counts(store, row["id"]),
        "created_at": row["created_at"],
    }


def cmd_projects_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    projects = [project_data(store, row, actor=args.actor) for row in store.rows("SELECT * FROM projects ORDER BY created_at, name")]
    return envelope("tri.project.list", command, actor=args.actor, start=start, data={"projects": projects})


def cmd_projects_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.project.inspect", command, actor=args.actor, start=start, data=project_data(store, store.get_project(args.project), actor=args.actor))


def package_boundary_data(row: sqlite3.Row, *, access: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "project": row["project_id"],
        "name": row["name"],
        "path_globs": loads(row["path_globs_json"], []),
        "visibility": loads(row["visibility_json"], []),
        "owners": loads(row["owners_json"], []),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
    if access is not None:
        data["access"] = access
    return data


def package_boundaries_data(store: Store, project_id: str, *, actor: str | None = None) -> tuple[list[dict[str, Any]], int]:
    packages: list[dict[str, Any]] = []
    redacted = 0
    for row in store.rows("SELECT * FROM package_boundaries WHERE project_id = ? ORDER BY name", (project_id,)):
        access = None
        if actor is not None:
            access, _ = object_access_decision(store, row["id"], actor=actor, action="package.read")
            if not access["allowed"]:
                redacted += 1
                continue
        packages.append(package_boundary_data(row, access=access))
    return packages, redacted


def resolve_package_boundary(store: Store, project_id: str, name_or_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM package_boundaries WHERE project_id = ? AND (id = ? OR name = ?)", (project_id, name_or_id, name_or_id))
    if row is None:
        raise TriError("path_not_found", "Package boundary not found.", object=name_or_id)
    return row


def resolve_package_boundary_for_inspect(store: Store, project_or_id: str, name_or_id: str | None = None) -> sqlite3.Row:
    if name_or_id is not None:
        project = store.get_project(project_or_id)
        return resolve_package_boundary(store, project["id"], name_or_id)
    if not project_or_id.startswith("pkg_"):
        raise TriError(
            "invalid_command",
            "Inspecting a package boundary by name requires a project.",
            object=project_or_id,
            usage="tri packages inspect <project> <name-or-id>",
        )
    row = store.row("SELECT * FROM package_boundaries WHERE id = ?", (project_or_id,))
    if row is None:
        raise TriError("path_not_found", "Package boundary not found.", object=project_or_id)
    return row


def package_hidden_patterns(store: Store, project_id: str, package_refs: list[str] | None, *, actor: str | None = None) -> list[str]:
    patterns: list[str] = []
    for package_ref in package_refs or []:
        row = resolve_package_boundary(store, project_id, package_ref)
        if actor is not None:
            require_object_access(store, row["id"], actor=actor, action="package.read")
        patterns.extend(loads(row["path_globs_json"], []))
    return normalize_hidden_patterns(patterns)


def package_visibility_hidden_patterns(store: Store, project_id: str, actor: str | None) -> list[str]:
    if actor is None:
        return []
    patterns: list[str] = []
    for row in store.rows("SELECT * FROM package_boundaries WHERE project_id = ? ORDER BY name", (project_id,)):
        decision, _ = object_access_decision(store, row["id"], actor=actor, action="package.read")
        if not decision["allowed"]:
            patterns.extend(loads(row["path_globs_json"], []))
    return normalize_hidden_patterns(patterns)


def effective_hidden_patterns(store: Store, project_id: str, hidden_patterns: list[str] | None = None, *, actor: str | None = None) -> list[str]:
    return sorted(set(normalize_hidden_patterns(hidden_patterns or []) + package_visibility_hidden_patterns(store, project_id, actor)))


def package_split_points_for_paths(store: Store, project_id: str, paths: list[str]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for row in store.rows("SELECT * FROM package_boundaries WHERE project_id = ? ORDER BY name", (project_id,)):
        package = package_boundary_data(row)
        matched_paths = [path for path in paths if path_matches_filters(path, package["path_globs"])]
        if matched_paths:
            suggestions.append(
                {
                    "kind": "package",
                    "package": package["id"],
                    "name": package["name"],
                    "paths": package["path_globs"],
                    "matched_paths": matched_paths,
                    "visibility": package["visibility"],
                    "reason": "package boundary can review separately",
                }
            )
    return suggestions


def cmd_packages_declare(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can declare package boundaries.", object=args.project, action="package.declare", required_grant="role:maintainer")
    project = store.get_project(args.project)
    path_globs = normalize_path_patterns(args.path or [])
    if not path_globs:
        raise TriError("invalid_command", "A package boundary requires at least one --path glob.", object=args.name)
    visibility = args.visibility or ["public"]
    owners = [normalize_principal(owner) for owner in (args.owner or [])]
    package_id = new_id("pkg")
    with store.conn:
        existing = store.row("SELECT id FROM package_boundaries WHERE project_id = ? AND name = ?", (project["id"], args.name))
        if existing is not None:
            raise TriError("invalid_command", "A package boundary with this name already exists.", object=args.name)
        store.conn.execute(
            """
            INSERT INTO package_boundaries (
              id, project_id, name, path_globs_json, visibility_json, owners_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (package_id, project["id"], args.name, dumps(path_globs), dumps(visibility), dumps(owners), args.actor, utcnow()),
        )
        evt = store.create_event(
            kind="package.declared",
            actor=args.actor,
            project_id=project["id"],
            object_refs={"package": package_id, "project": project["id"], "paths": path_globs},
        )
    package = resolve_package_boundary(store, project["id"], package_id)
    access, _ = object_access_decision(store, package["id"], actor=args.actor, action="package.read")
    return envelope("tri.package.inspect", command, actor=args.actor, start=start, events=[evt], data=package_boundary_data(package, access=access))


def cmd_packages_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    packages, redacted = package_boundaries_data(store, project["id"], actor=args.actor)
    return envelope("tri.package.list", command, actor=args.actor, start=start, data={"project": project["id"], "packages": packages, "redacted": redacted})


def cmd_packages_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    package = resolve_package_boundary_for_inspect(store, args.project, args.name)
    access = require_object_access(store, package["id"], actor=args.actor, action="package.read")
    return envelope("tri.package.inspect", command, actor=args.actor, start=start, data=package_boundary_data(package, access=access))


def cmd_targets_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    return envelope("tri.targets.list", command, actor=args.actor, start=start, data={"project": project["id"], "targets": project_targets_data(store, project["id"])})


def cmd_targets_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = resolve_target_for_inspect(store, args.project, args.target)
    return envelope("tri.targets.inspect", command, actor=args.actor, start=start, data=target_data(store, row, actor=args.actor))


def snapshot_access_data(store: Store, snapshot: str, *, actor: str) -> dict[str, Any]:
    if snapshot_has_public_source(store, snapshot):
        return {
            "allowed": True,
            "actor": actor,
            "action": "snapshot.read",
            "object": snapshot,
            "reason": "public_source",
            "origins": [],
            "redacted_origins": 0,
        }
    origins = []
    redacted_origins = 0
    allowed = False
    reason = "missing_allow"
    for object_id, action in snapshot_access_candidates(store, snapshot):
        decision, _ = object_access_decision(store, object_id, actor=actor, action=action)
        if decision["allowed"]:
            allowed = True
            reason = "origin_access"
            origins.append({"object": object_id, "action": action, "reason": decision["reason"], "grants": decision["grants"]})
        else:
            redacted_origins += 1
    return {
        "allowed": allowed,
        "actor": actor,
        "action": "snapshot.read",
        "object": snapshot,
        "reason": reason,
        "origins": origins,
        "redacted_origins": redacted_origins,
    }


def visible_snapshot_refs(store: Store, snapshot: str, *, actor: str) -> tuple[dict[str, list[str]], dict[str, int]]:
    refs = {
        "targets": [row["id"] for row in store.rows("SELECT id FROM targets WHERE snapshot_id = ? ORDER BY name", (snapshot,))],
        "workspaces": [],
        "changesets": [],
        "checks": [],
        "releases": [],
    }
    redacted = {"workspaces": 0, "changesets": 0, "checks": 0, "releases": 0, "source_events": 0}
    seen_workspaces: set[str] = set()
    for row in store.rows(
        "SELECT DISTINCT id FROM workspaces WHERE base_snapshot_id = ? OR head_snapshot_id = ? OR materialized_snapshot_id = ? ORDER BY id",
        (snapshot, snapshot, snapshot),
    ):
        seen_workspaces.add(row["id"])
        decision, _ = object_access_decision(store, row["id"], actor=actor, action="workspace.read")
        if decision["allowed"]:
            refs["workspaces"].append(row["id"])
        else:
            redacted["workspaces"] += 1
    for row in store.rows(
        "SELECT DISTINCT workspace_id FROM operations WHERE input_snapshot_id = ? OR output_snapshot_id = ? ORDER BY workspace_id",
        (snapshot, snapshot),
    ):
        if row["workspace_id"] in seen_workspaces:
            continue
        seen_workspaces.add(row["workspace_id"])
        decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
        if decision["allowed"]:
            refs["workspaces"].append(row["workspace_id"])
        else:
            redacted["workspaces"] += 1
    for row in store.rows("SELECT DISTINCT id FROM changesets WHERE base_snapshot_id = ? OR head_snapshot_id = ? ORDER BY id", (snapshot, snapshot)):
        decision, _ = object_access_decision(store, row["id"], actor=actor, action="changeset.read")
        if decision["allowed"]:
            refs["changesets"].append(row["id"])
        else:
            redacted["changesets"] += 1
    for row in store.rows("SELECT DISTINCT id FROM check_runs WHERE snapshot_before = ? OR snapshot_after = ? ORDER BY id", (snapshot, snapshot)):
        decision = check_access_decision(store, check_data(store, row["id"]), actor=actor)
        if decision["allowed"]:
            refs["checks"].append(row["id"])
        else:
            redacted["checks"] += 1
    for row in store.rows("SELECT DISTINCT id FROM releases WHERE target_snapshot_id = ? ORDER BY id", (snapshot,)):
        decision, _ = object_access_decision(store, row["id"], actor=actor, action="release.read")
        if decision["allowed"]:
            refs["releases"].append(row["id"])
        else:
            redacted["releases"] += 1
    return refs, redacted


def snapshot_event_ids(store: Store, snapshot: str, *, actor: str) -> tuple[list[str], int]:
    rows = store.rows(
        """
        SELECT * FROM events
        WHERE object_refs_json LIKE ? OR input_fingerprints_json LIKE ? OR output_fingerprints_json LIKE ?
        ORDER BY created_at, rowid
        """,
        (f"%{snapshot}%", f"%{snapshot}%", f"%{snapshot}%"),
    )
    events = []
    redacted = 0
    for row in rows:
        try:
            events.append(event_view(store, row, actor=actor, record_denial=False)["id"])
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return events, redacted


def snapshot_data(store: Store, snapshot: str, *, actor: str) -> dict[str, Any]:
    row = snapshot_row(store, snapshot)
    require_snapshot_access(store, snapshot, actor=actor, action="snapshot.read")
    entries = store.load_tree(row["root_tree_id"])
    refs, redacted = visible_snapshot_refs(store, snapshot, actor=actor)
    source_events, redacted_events = snapshot_event_ids(store, snapshot, actor=actor)
    redacted["source_events"] = redacted_events
    return {
        "id": row["id"],
        "project": row["project_id"],
        "root_tree": row["root_tree_id"],
        "parents": loads(row["parent_ids_json"], []),
        "source": row["source"],
        "content_fingerprint": row["content_fingerprint"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "metadata": loads(row["metadata_json"], {}),
        "file_count": len(entries),
        "byte_count": sum(int(entry.get("size", 0)) for entry in entries.values()),
        "targets": refs["targets"],
        "workspaces": refs["workspaces"],
        "changesets": refs["changesets"],
        "checks": refs["checks"],
        "releases": refs["releases"],
        "source_events": source_events,
        "redacted": redacted,
        "access": snapshot_access_data(store, snapshot, actor=actor),
    }


def cmd_snapshots_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.snapshot.inspect", command, actor=args.actor, start=start, data=snapshot_data(store, args.snapshot, actor=args.actor))


def cmd_snapshots_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    snapshots = []
    redacted = 0
    for row in store.rows("SELECT id FROM snapshots WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],)):
        try:
            snapshots.append(snapshot_data(store, row["id"], actor=args.actor))
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return envelope("tri.snapshot.list", command, actor=args.actor, start=start, data={"project": project["id"], "snapshots": snapshots, "redacted": redacted})


def advisory_is_disclosed(row: sqlite3.Row) -> bool:
    return row["status"] == "disclosed" or row["disclosure_state"] in ("public", "disclosed")


def resolve_advisory(store: Store, project_id: str, advisory_ref: str) -> sqlite3.Row:
    row = store.row(
        "SELECT * FROM advisories WHERE project_id = ? AND (id = ? OR title = ?) ORDER BY created_at LIMIT 1",
        (project_id, advisory_ref, advisory_ref),
    )
    if row is None:
        raise TriError("path_not_found", "Advisory not found.", object=advisory_ref)
    return row


def advisory_data(store: Store, row: sqlite3.Row, *, actor: str | None = None) -> dict[str, Any]:
    private_changesets = loads(row["private_changesets_json"], [])
    redacted = {"private_changesets": 0}
    if actor is not None:
        visible_changesets = []
        for changeset_id in private_changesets:
            decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
            if decision["allowed"]:
                visible_changesets.append(changeset_id)
            else:
                redacted["private_changesets"] += 1
        private_changesets = visible_changesets
    return {
        "id": row["id"],
        "project": row["project_id"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "disclosure_state": row["disclosure_state"],
        "embargoed": row["disclosure_state"] == "embargoed",
        "affected_packages": loads(row["affected_packages_json"], []),
        "trusted_reviewers": loads(row["trusted_reviewers_json"], []),
        "private_changesets": private_changesets,
        "redacted": redacted,
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "disclosed_at": row["disclosed_at"],
    }


def create_advisory_record(
    store: Store,
    *,
    project: sqlite3.Row,
    actor: str,
    title: str,
    body: str,
    affected_packages: list[str],
    trusted_reviewers: list[str],
    embargoed: bool,
    event_kind: str,
) -> tuple[dict[str, Any], str]:
    advisory_id = new_id("adv")
    disclosure_state = "embargoed" if embargoed else "public"
    with store.conn:
        store.conn.execute(
            """
            INSERT INTO advisories (
              id, project_id, title, body, status, disclosure_state, affected_packages_json,
              trusted_reviewers_json, private_changesets_json, created_by, created_at, disclosed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                advisory_id,
                project["id"],
                title,
                body,
                "reported",
                disclosure_state,
                dumps(affected_packages),
                dumps(trusted_reviewers),
                dumps([]),
                actor,
                utcnow(),
                None if disclosure_state == "embargoed" else utcnow(),
            ),
        )
        evt = store.create_event(
            kind=event_kind,
            actor=actor,
            project_id=project["id"],
            object_refs={"advisory": advisory_id, "affected_packages": affected_packages, "disclosure_state": disclosure_state},
        )
    return advisory_data(store, store.get_advisory(advisory_id), actor=actor), evt


def cmd_advisory_create(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    body = Path(args.body_file).read_text() if args.body_file else (args.body or "")
    data, evt = create_advisory_record(
        store,
        project=project,
        actor=args.actor,
        title=args.title,
        body=body,
        affected_packages=sorted(set(args.affected_package or [])),
        trusted_reviewers=sorted({normalize_principal(item) for item in args.trusted_reviewer or []}),
        embargoed=args.embargoed,
        event_kind="advisory.created",
    )
    return envelope("tri.advisory.inspect", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_advisory_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    advisories = []
    redacted = 0
    for row in store.rows("SELECT * FROM advisories WHERE project_id = ? ORDER BY created_at", (project["id"],)):
        decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="advisory.read")
        if decision["allowed"]:
            advisories.append(advisory_data(store, row, actor=args.actor))
        else:
            redacted += 1
    return envelope("tri.advisory.list", command, actor=args.actor, start=start, data={"project": project["id"], "advisories": advisories, "redacted": redacted})


def cmd_advisory_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_object_access(store, args.advisory, actor=args.actor, action="advisory.read")
    return envelope("tri.advisory.inspect", command, actor=args.actor, start=start, data=advisory_data(store, store.get_advisory(args.advisory), actor=args.actor))


def actor_can_read_release_snapshot(store: Store, changeset_ids: list[str], *, actor: str | None) -> bool:
    if actor is None:
        return True
    if "role:maintainer" in actor_selectors(actor):
        return True
    for changeset_id in changeset_ids:
        try:
            decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
        except TriError:
            continue
        if decision["allowed"]:
            return True
    return False


def artifact_visible_without_release_origin(store: Store, artifact_id: str, *, actor: str) -> bool:
    origins = [origin for origin in artifact_origins(store, artifact_id) if origin.get("type") != "release"]
    if "role:maintainer" in actor_selectors(actor):
        return True
    for origin in origins:
        policy_object = origin.get("policy_object")
        policy_action = origin.get("policy_action")
        if not policy_object or not policy_action:
            continue
        try:
            decision, _ = object_access_decision(store, policy_object, actor=actor, action=policy_action)
        except TriError:
            continue
        if decision["allowed"]:
            return True
    return False


def release_data(store: Store, row: sqlite3.Row, *, actor: str | None = None) -> dict[str, Any]:
    changesets = loads(row["included_changesets_json"], [])
    checks = loads(row["checks_json"], [])
    artifacts = loads(row["artifacts_json"], [])
    advisories = loads(row["advisories_disclosed_json"], [])
    policy_decisions = loads(row["policy_decisions_json"], [])
    redacted = {
        "included_changesets": 0,
        "checks": 0,
        "artifacts": 0,
        "advisories_disclosed": 0,
        "policy_decisions": 0,
    }
    target_snapshot = row["target_snapshot_id"]
    if actor is not None:
        visible_changesets = []
        for changeset_id in changesets:
            decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
            if decision["allowed"]:
                visible_changesets.append(changeset_id)
            else:
                redacted["included_changesets"] += 1
        changesets = visible_changesets

        visible_checks = []
        for check_id in checks:
            decision = check_access_decision(store, check_data(store, check_id), actor=actor)
            if decision["allowed"]:
                visible_checks.append(check_id)
            else:
                redacted["checks"] += 1
        checks = visible_checks

        visible_artifacts = []
        for artifact_id in artifacts:
            if artifact_visible_without_release_origin(store, artifact_id, actor=actor):
                visible_artifacts.append(artifact_id)
            else:
                redacted["artifacts"] += 1
        artifacts = visible_artifacts

        visible_advisories = []
        for advisory_id in advisories:
            decision, _ = object_access_decision(store, advisory_id, actor=actor, action="advisory.read")
            if decision["allowed"]:
                visible_advisories.append(advisory_id)
            else:
                redacted["advisories_disclosed"] += 1
        advisories = visible_advisories

        if not actor_can_read_release_snapshot(store, loads(row["included_changesets_json"], []), actor=actor):
            target_snapshot = None
        redacted_policy_refs = [0]
        policy_decisions = redact_policy_payload_refs(store, policy_decisions, actor=actor, counter=redacted_policy_refs)
        redacted["policy_decisions"] = redacted_policy_refs[0]
    return {
        "id": row["id"],
        "project": row["project_id"],
        "status": row["status"],
        "target_snapshot": target_snapshot,
        "target_snapshot_redacted": target_snapshot is None and row["target_snapshot_id"] is not None,
        "included_changesets": changesets,
        "packages": loads(row["packages_json"], []),
        "artifacts": artifacts,
        "checks": checks,
        "advisories_disclosed": advisories,
        "redacted": redacted,
        "rollback_target": row["rollback_target"],
        "policy_decisions": policy_decisions,
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def release_evidence_for_changesets(store: Store, changesets: list[sqlite3.Row]) -> tuple[list[str], list[str]]:
    checks: list[str] = []
    artifacts: list[str] = []
    for cs in changesets:
        for check_id in loads(cs["checks_json"], []):
            if check_id not in checks:
                checks.append(check_id)
            check = check_data(store, check_id)
            for artifact_id in [check["logs"]["stdout"], check["logs"]["stderr"], *check["artifacts"]]:
                if artifact_id and artifact_id not in artifacts:
                    artifacts.append(artifact_id)
    return checks, artifacts


def release_candidate_data(
    *,
    project_id: str,
    status: str,
    changesets: list[sqlite3.Row],
    packages: list[str],
    artifacts: list[str],
    checks: list[str],
    advisories_disclosed: list[str],
    rollback_target: str | None,
    policy_decisions: list[dict[str, Any]],
    actor: str,
    created_at: str,
) -> dict[str, Any]:
    target_snapshot = changesets[-1]["head_snapshot_id"] if changesets else None
    return {
        "id": None,
        "project": project_id,
        "status": status,
        "target_snapshot": target_snapshot,
        "target_snapshot_redacted": False,
        "included_changesets": [cs["id"] for cs in changesets],
        "packages": packages,
        "artifacts": artifacts,
        "checks": checks,
        "advisories_disclosed": advisories_disclosed,
        "redacted": {
            "included_changesets": 0,
            "checks": 0,
            "artifacts": 0,
            "advisories_disclosed": 0,
        },
        "rollback_target": rollback_target,
        "policy_decisions": policy_decisions,
        "created_by": actor,
        "created_at": created_at,
    }


def prepare_release(store: Store, args: argparse.Namespace, command: list[str], start: float, *, schema: str) -> dict[str, Any]:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can prepare releases.", action="release.prepare")
    project = store.get_project(args.project)
    changesets = [store.get_changeset(changeset_id) for changeset_id in args.include]
    for cs in changesets:
        if cs["project_id"] != project["id"]:
            raise TriError("invalid_command", "Included changeset belongs to a different project.", object=cs["id"], project=project["id"])
    disclosed_advisories = [resolve_advisory(store, project["id"], advisory_ref) for advisory_ref in args.disclose or []]
    disclosed_ids = {row["id"] for row in disclosed_advisories}
    policy_events: list[str] = []
    policy_decisions: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for cs in changesets:
        decision, decision_diagnostics = evaluate_export_policy(
            store,
            cs,
            actor=args.actor,
            action="release.prepare",
            disclosed_advisories=disclosed_ids,
        )
        policy_decisions.append(decision)
        policy_events.append(store.record_policy_decision(actor=args.actor, action="release.prepare", object_id=cs["id"], decision=decision, project_id=cs["project_id"], workspace_id=cs["workspace_id"]))
        diagnostics.extend(decision_diagnostics)
    packages = sorted(set(args.package or []))
    checks, artifacts = release_evidence_for_changesets(store, changesets)
    if diagnostics:
        data = release_candidate_data(
            project_id=project["id"],
            status="policy_blocked",
            changesets=changesets,
            packages=packages,
            artifacts=artifacts,
            checks=checks,
            advisories_disclosed=sorted(disclosed_ids),
            rollback_target=args.rollback_target,
            policy_decisions=policy_decisions,
            actor=args.actor,
            created_at=utcnow(),
        )
        return envelope(schema, command, actor=args.actor, start=start, ok=False, events=policy_events, diagnostics=diagnostics, data=data)

    release_id = new_id("rel")
    now = utcnow()
    advisory_events: list[str] = []
    with store.conn:
        for advisory in disclosed_advisories:
            store.conn.execute(
                "UPDATE advisories SET status = ?, disclosure_state = ?, disclosed_at = ? WHERE id = ?",
                ("disclosed", "disclosed", now, advisory["id"]),
            )
            advisory_events.append(
                store.create_event(
                    kind="advisory.disclosed",
                    actor=args.actor,
                    project_id=project["id"],
                    object_refs={"advisory": advisory["id"], "release": release_id},
                )
            )
        store.conn.execute(
            """
            INSERT INTO releases (
              id, project_id, status, target_snapshot_id, included_changesets_json, packages_json,
              artifacts_json, checks_json, advisories_disclosed_json, rollback_target, policy_decisions_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release_id,
                project["id"],
                "prepared",
                changesets[-1]["head_snapshot_id"],
                dumps([cs["id"] for cs in changesets]),
                dumps(packages),
                dumps(artifacts),
                dumps(checks),
                dumps(sorted(disclosed_ids)),
                args.rollback_target,
                dumps(policy_decisions),
                args.actor,
                now,
            ),
        )
        release_evt = store.create_event(
            kind="release.prepared",
            actor=args.actor,
            project_id=project["id"],
            object_refs={"release": release_id, "changesets": [cs["id"] for cs in changesets], "advisories_disclosed": sorted(disclosed_ids)},
            output_fingerprints={"target_snapshot": changesets[-1]["head_snapshot_id"]},
        )
    data = release_data(store, store.get_release(release_id), actor=args.actor)
    data["policy_decisions"] = policy_decisions
    return envelope(schema, command, actor=args.actor, start=start, events=[*policy_events, *advisory_events, release_evt], data=data)


def cmd_release_prepare(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return prepare_release(store, args, command, start, schema="tri.release.inspect")


def cmd_release_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    releases = []
    redacted = 0
    for row in store.rows("SELECT * FROM releases WHERE project_id = ? ORDER BY created_at", (project["id"],)):
        decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="release.read")
        if decision["allowed"]:
            releases.append(release_data(store, row, actor=args.actor))
        else:
            redacted += 1
    return envelope("tri.release.list", command, actor=args.actor, start=start, data={"project": project["id"], "releases": releases, "redacted": redacted})


def cmd_release_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_object_access(store, args.release, actor=args.actor, action="release.read")
    return envelope("tri.release.inspect", command, actor=args.actor, start=start, data=release_data(store, store.get_release(args.release), actor=args.actor))


def cmd_task_create(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_not_task_scoped_actor(args.actor, action="task.create", object=args.project)
    project = store.get_project(args.project)
    task_id = new_id("task")
    body = Path(args.body_file).read_text() if args.body_file else (args.body or "")
    acceptance = list(args.acceptance or [])
    inline_checks = parse_required_checks(args.required_check or [])
    set_checks = checks_from_sets(store, project["id"], args.check_set)
    required_checks = merge_required_checks(set_checks, inline_checks)
    with store.conn:
        store.conn.execute(
            "INSERT INTO tasks (id, project_id, title, body, requester, acceptance_criteria_json, required_checks_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project["id"], args.title, body, args.actor, dumps(acceptance), dumps(required_checks), "open", utcnow()),
        )
        evt = store.create_event(
            kind="task.created",
            actor=args.actor,
            project_id=project["id"],
            task_id=task_id,
            object_refs={"task": task_id, "required_checks": required_checks},
        )
    return envelope("tri.task.inspect", command, actor=args.actor, start=start, events=[evt], data=task_data(store, task_id, actor=args.actor))


def parse_required_checks(values: list[str]) -> list[dict[str, Any]]:
    checks = []
    for value in values:
        cmd = shlex.split(value)
        if not cmd:
            raise TriError("invalid_command", "Required check command cannot be empty.")
        checks.append({"name": " ".join(cmd), "command": cmd})
    return checks


def check_set_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": row["project_id"],
        "name": row["name"],
        "required_checks": loads(row["required_checks_json"], []),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def resolve_check_set(store: Store, project_id: str, name_or_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM check_sets WHERE project_id = ? AND (id = ? OR name = ?)", (project_id, name_or_id, name_or_id))
    if row is None:
        raise TriError("path_not_found", "Required-check set not found.", object=name_or_id)
    return row


def resolve_check_set_for_inspect(store: Store, project_or_id: str, name_or_id: str | None = None) -> sqlite3.Row:
    if name_or_id is not None:
        project = store.get_project(project_or_id)
        return resolve_check_set(store, project["id"], name_or_id)
    if not project_or_id.startswith("checkset_"):
        raise TriError(
            "invalid_command",
            "Inspecting a check set by name requires a project.",
            object=project_or_id,
            usage="tri checks set-inspect <project> <name-or-id>",
        )
    row = store.row("SELECT * FROM check_sets WHERE id = ?", (project_or_id,))
    if row is None:
        raise TriError("path_not_found", "Required-check set not found.", object=project_or_id)
    return row


def checks_from_sets(store: Store, project_id: str, names: list[str] | None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name in names or []:
        check_set = check_set_data(resolve_check_set(store, project_id, name))
        for check in check_set["required_checks"]:
            checks.append({**check, "source": {"type": "check_set", "id": check_set["id"], "name": check_set["name"]}})
    return checks


def merge_required_checks(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for check in group:
            key = dumps(check["command"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(check)
    return merged


def normalize_assumptions(values: list[str] | None) -> list[str]:
    assumptions: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        assumptions.append(text)
    return assumptions


def merge_assumptions(existing: list[str], additions: list[str]) -> list[str]:
    return normalize_assumptions([*existing, *additions])


def attach_assumptions_to_task_run(store: Store, task_run_id: str | None, assumptions: list[str]) -> None:
    if not task_run_id or not assumptions:
        return
    row = store.row("SELECT assumptions_json FROM task_runs WHERE id = ?", (task_run_id,))
    if row is None:
        return
    merged = merge_assumptions(loads(row["assumptions_json"], []), assumptions)
    store.conn.execute("UPDATE task_runs SET assumptions_json = ? WHERE id = ?", (dumps(merged), task_run_id))


def task_data(store: Store, task_id: str, *, actor: str | None = None) -> dict[str, Any]:
    task = store.get_task(task_id)
    workspaces = []
    redacted = 0
    all_workspace_ids = [row["id"] for row in store.rows("SELECT id FROM workspaces WHERE task_id = ? ORDER BY created_at", (task_id,))]
    for workspace_id in all_workspace_ids:
        if actor is None:
            workspaces.append(workspace_id)
            continue
        decision, _ = object_access_decision(store, workspace_id, actor=actor, action="workspace.read")
        if decision["allowed"]:
            workspaces.append(workspace_id)
        else:
            redacted += 1
    runs = []
    for row in store.rows("SELECT * FROM task_runs WHERE task_id = ? ORDER BY created_at", (task_id,)):
        run = task_run_view(store, row, actor=actor) if actor is not None else task_run_data(row)
        if run.get("redacted_workspace"):
            redacted += 1
            continue
        runs.append(run["id"])
    changesets = []
    for workspace_id in all_workspace_ids:
        changesets.extend(row["id"] for row in store.rows("SELECT id FROM changesets WHERE workspace_id = ? ORDER BY created_at", (workspace_id,)))
    if actor is not None:
        visible_changesets = []
        for changeset_id in changesets:
            decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
            if decision["allowed"]:
                visible_changesets.append(changeset_id)
            else:
                redacted += 1
        changesets = visible_changesets
    return {
        "id": task["id"],
        "project": task["project_id"],
        "title": task["title"],
        "body": task["body"],
        "requester": task["requester"],
        "status": task["status"],
        "acceptance_criteria": loads(task["acceptance_criteria_json"], []),
        "required_checks": loads(task["required_checks_json"], []),
        "lifecycle": loads(task["lifecycle_json"], {}),
        "workspaces": workspaces,
        "runs": runs,
        "changesets": changesets,
        "redacted": redacted,
        "created_at": task["created_at"],
    }


def task_run_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task": row["task_id"],
        "actor": row["actor"],
        "workspace": row["workspace_id"],
        "redacted_workspace": False,
        "status": row["status"],
        "final_recommendation": row["final_recommendation"],
        "assumptions": loads(row["assumptions_json"], []),
        "created_at": row["created_at"],
    }


def empty_task_run_cost() -> dict[str, Any]:
    return {"wall_ms": 0, "input_bytes": 0, "output_bytes": 0, "external_cost": "0 USD"}


def add_task_run_cost(summary: dict[str, Any], cost: dict[str, Any]) -> None:
    for key in ("wall_ms", "input_bytes", "output_bytes"):
        value = cost.get(key, 0)
        if isinstance(value, int):
            summary[key] += value
    external = cost.get("external_cost")
    if external and external != "0 USD":
        existing = summary.get("_external_costs", [])
        existing.append(str(external))
        summary["_external_costs"] = existing


def finalize_task_run_cost(summary: dict[str, Any]) -> dict[str, Any]:
    external_costs = sorted(set(summary.pop("_external_costs", [])))
    if external_costs:
        summary["external_cost"] = ", ".join(external_costs)
    return summary


def task_run_evidence(store: Store, run_id: str, *, actor: str | None) -> dict[str, Any]:
    commands = []
    checks = []
    artifacts = []
    changesets = []
    artifact_seen: set[str] = set()
    cost = empty_task_run_cost()
    redacted = {"checks": 0, "changesets": 0}
    for row in store.rows("SELECT id FROM check_runs WHERE task_run_id = ? ORDER BY created_at, rowid", (run_id,)):
        check = check_data(store, row["id"])
        if actor is not None:
            decision = check_access_decision(store, check, actor=actor)
            if not decision["allowed"]:
                redacted["checks"] += 1
                continue
            check = check_view(store, row["id"], actor=actor)
        checks.append(check)
        command_artifacts = []
        for artifact_id in [*check.get("logs", {}).values(), *check.get("artifacts", [])]:
            if not isinstance(artifact_id, str):
                continue
            command_artifacts.append(artifact_id)
            if artifact_id not in artifact_seen:
                artifact_seen.add(artifact_id)
                artifacts.append(artifact_id)
        commands.append(
            {
                "check": check["id"],
                "command": check["command"],
                "status": check["status"],
                "exit_status": check["exit_status"],
                "duration_ms": check["duration_ms"],
                "snapshot_before": check["snapshot_before"],
                "snapshot_after": check["snapshot_after"],
                "logs": check["logs"],
                "artifacts": command_artifacts,
                "cost": check["cost"],
                "assumptions": check["assumptions"],
            }
        )
        add_task_run_cost(cost, check.get("cost", {}))
    for row in store.rows("SELECT id FROM changesets WHERE task_run_id = ? ORDER BY created_at, rowid", (run_id,)):
        if actor is not None:
            decision, _ = object_access_decision(store, row["id"], actor=actor, action="changeset.read")
            if not decision["allowed"]:
                redacted["changesets"] += 1
                continue
        changesets.append(row["id"])
    return {
        "commands": commands,
        "checks": checks,
        "artifacts": artifacts,
        "changesets": changesets,
        "cost": finalize_task_run_cost(cost),
        "redacted": redacted,
    }


def task_run_view(store: Store, row: sqlite3.Row, *, actor: str | None = None) -> dict[str, Any]:
    data = task_run_data(row)
    if actor is None or not row["workspace_id"]:
        data["redacted_workspace"] = False
    else:
        decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
        data["redacted_workspace"] = not decision["allowed"]
        if data["redacted_workspace"]:
            data["workspace"] = None
    data.update(task_run_evidence(store, row["id"], actor=actor))
    return data


def get_task_run(store: Store, run_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM task_runs WHERE id = ?", (run_id,))
    if row is None:
        raise TriError("path_not_found", "Task run not found.", object=run_id)
    return row


def task_run_event_context(store: Store, row: sqlite3.Row) -> dict[str, str | None]:
    workspace_id = row["workspace_id"]
    task_id = row["task_id"]
    project_id = None
    if workspace_id:
        try:
            workspace = store.get_workspace(workspace_id)
            project_id = workspace["project_id"]
            task_id = task_id or workspace["task_id"]
        except TriError:
            pass
    if project_id is None and task_id:
        try:
            project_id = store.get_task(task_id)["project_id"]
        except TriError:
            pass
    return {"project": project_id, "workspace": workspace_id, "task": task_id}


def task_run_required_grant(row: sqlite3.Row, *, workspace_denied: bool) -> str:
    if row["workspace_id"] and not row["task_id"]:
        return "workspace.read"
    if workspace_denied and row["task_id"]:
        return "workspace.read or task.read"
    return "task.read"


def task_run_policy_candidate(object_id: str, action: str, decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": object_id,
        "action": action,
        "allowed": decision["allowed"],
        "reason": decision.get("reason"),
        "grants": decision.get("grants", []),
        "matched_grants": decision.get("matched_grants", []),
    }


def task_run_access_decision(store: Store, row: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    selectors = actor_selectors(actor)
    candidates: list[dict[str, Any]] = []
    grants: list[str] = []
    matched_grants: list[dict[str, Any]] = []
    allowed = False
    reason = "missing_allow"
    workspace_denied = False

    if row["workspace_id"]:
        workspace_decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
        candidates.append(task_run_policy_candidate(row["workspace_id"], "workspace.read", workspace_decision))
        if workspace_decision["allowed"]:
            allowed = True
            reason = "workspace_access"
            grants.extend(workspace_decision.get("grants", []))
            matched_grants.extend(workspace_decision.get("matched_grants", []))
        else:
            workspace_denied = True
    if not allowed and row["task_id"]:
        task_decision, _ = object_access_decision(store, row["task_id"], actor=actor, action="task.read")
        candidates.append(task_run_policy_candidate(row["task_id"], "task.read", task_decision))
        if task_decision["allowed"]:
            allowed = True
            reason = "task_access"
            grants.extend(task_decision.get("grants", []))
            matched_grants.extend(task_decision.get("matched_grants", []))
    required_grant = task_run_required_grant(row, workspace_denied=workspace_denied)
    return {
        "id": new_id("decision"),
        "allowed": allowed,
        "actor": actor,
        "action": "task_run.read",
        "object": row["id"],
        "object_type": "task_run",
        "policies": ["local_policy_v0"],
        "visibility": ["private"],
        "owner": row["actor"],
        "workspace": row["workspace_id"],
        "task": row["task_id"],
        "task_scope": actor_task_scope(actor),
        "actor_selectors": selectors,
        "policy_candidates": candidates,
        "grants": sorted(set(grants)),
        "matched_grants": matched_grants,
        "denials": [] if allowed else ["policy_denied"],
        "redactions": [],
        "required_grant": required_grant,
        "reason_code": "allowed" if allowed else "policy_denied",
        "reason": reason,
        "event": None,
    }


def task_run_access_diagnostics(decision: dict[str, Any]) -> list[dict[str, Any]]:
    if decision["allowed"]:
        return []
    return [
        decision_denial(
            "policy_denied",
            "Actor is not allowed to access this task run.",
            object=decision["object"],
            action="task_run.read",
            required_grant=decision["required_grant"],
        )
    ]


def task_run_access_decision_view(store: Store, row: sqlite3.Row, decision: dict[str, Any], *, actor: str) -> dict[str, Any]:
    view = copy.deepcopy(decision)
    redacted_parent_refs = 0
    if row["workspace_id"]:
        workspace_decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
        if not workspace_decision["allowed"]:
            view["workspace"] = None
            view["redacted_workspace"] = True
            redacted_parent_refs += 1
        else:
            view["redacted_workspace"] = False
    if row["task_id"]:
        task_decision, _ = object_access_decision(store, row["task_id"], actor=actor, action="task.read")
        if not task_decision["allowed"]:
            view["task"] = None
            view["redacted_task"] = True
            redacted_parent_refs += 1
        else:
            view["redacted_task"] = False
    candidates = []
    for candidate in view.get("policy_candidates", []):
        candidate = copy.deepcopy(candidate)
        object_id = candidate.get("object")
        action = candidate.get("action")
        if isinstance(object_id, str) and isinstance(action, str):
            try:
                candidate_decision, _ = object_access_decision(store, object_id, actor=actor, action=action)
            except TriError:
                candidate_decision = {"allowed": False}
            if not candidate_decision["allowed"]:
                candidate["object"] = None
                candidate["object_redacted"] = True
                candidate["required_grant"] = action
        candidates.append(candidate)
    view["policy_candidates"] = candidates
    view["redacted_parent_refs"] = redacted_parent_refs
    return view


def task_run_policy_visibility_data(store: Store, row: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    ctx = task_run_event_context(store, row)
    access_decision = task_run_access_decision(store, row, actor=actor)
    access_view = task_run_access_decision_view(store, row, access_decision, actor=actor)
    parents: dict[str, Any] = {}
    hidden_paths: list[str] = []
    inherited_grants: list[dict[str, Any]] = []
    if row["workspace_id"]:
        workspace_decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
        if workspace_decision["allowed"]:
            parents["workspace"] = row["workspace_id"]
            workspace = store.get_workspace(row["workspace_id"])
            hidden_paths = loads(workspace["hidden_paths_json"], [])
            inherited_grants.extend(active_grants_for_object(store, row["workspace_id"]))
        else:
            parents["workspace_redacted"] = True
    if row["task_id"]:
        task_decision, _ = object_access_decision(store, row["task_id"], actor=actor, action="task.read")
        if task_decision["allowed"]:
            parents["task"] = row["task_id"]
            inherited_grants.extend(active_grants_for_object(store, row["task_id"]))
        else:
            parents["task_redacted"] = True
    return {
        "object": row["id"],
        "object_type": "task_run",
        "project": ctx["project"],
        "workspace": access_view.get("workspace"),
        "task": access_view.get("task"),
        "parents": parents,
        "visibility": ["private"],
        "hidden_paths": hidden_paths,
        "owner": row["actor"],
        "actor": actor,
        "actor_selectors": actor_selectors(actor),
        "direct_grants": [],
        "inherited_grants": inherited_grants,
        "access": access_view,
        "required_grant": access_view["required_grant"],
        "redacted_parent_refs": access_view["redacted_parent_refs"],
    }


def require_task_run_access(store: Store, row: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    decision = task_run_access_decision(store, row, actor=actor)
    if decision["allowed"]:
        return decision
    ctx = task_run_event_context(store, row)
    evt = store.record_policy_decision(
        actor=actor,
        action="task_run.read",
        object_id=row["id"],
        decision=decision,
        project_id=ctx["project"],
        workspace_id=ctx["workspace"],
        task_id=ctx["task"],
        kind="policy.denied",
    )
    raise TriError(
        "policy_denied",
        "Actor is not allowed to access this task run.",
        object=row["id"],
        action="task_run.read",
        required_grant=decision["required_grant"],
        decision=decision["id"],
        event=evt,
        explain_command=["tri", "policy", "explain", decision["id"]],
    )


def workspace_ref_for_actor(store: Store, workspace_id: str | None, *, actor: str | None) -> tuple[str | None, bool]:
    if not workspace_id:
        return None, False
    if actor is None:
        return workspace_id, False
    decision, _ = object_access_decision(store, workspace_id, actor=actor, action="workspace.read")
    if decision["allowed"]:
        return workspace_id, False
    return None, True


def latest_workspace_task_run(store: Store, workspace_id: str) -> sqlite3.Row | None:
    return store.row("SELECT * FROM task_runs WHERE workspace_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (workspace_id,))


def set_workspace_task_run_phase(
    store: Store,
    workspace: sqlite3.Row,
    *,
    actor: str,
    status: str,
    reason: str,
) -> str | None:
    run = latest_workspace_task_run(store, workspace["id"])
    if run is None:
        return None
    if run["status"] in TASK_RUN_TERMINAL_STATUSES:
        raise TriError(
            "task_lifecycle_blocked",
            "Task run is terminal and cannot accept new work.",
            object=run["id"],
            status=run["status"],
            requested_status=status,
        )
    if run["status"] == status:
        return None
    store.conn.execute("UPDATE task_runs SET status = ? WHERE id = ?", (status, run["id"]))
    return store.create_event(
        kind=f"task_run.{status}",
        actor=actor,
        project_id=workspace["project_id"],
        workspace_id=workspace["id"],
        task_id=workspace["task_id"],
        object_refs={"task_run": run["id"], "previous_status": run["status"], "status": status, "reason": reason},
    )


def cmd_task_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_object_access(store, args.task, actor=args.actor, action="task.read")
    return envelope("tri.task.inspect", command, actor=args.actor, start=start, data=task_data(store, args.task, actor=args.actor))


def cmd_task_run_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = get_task_run(store, args.run)
    require_task_run_access(store, row, actor=args.actor)
    return envelope("tri.task_run.inspect", command, actor=args.actor, start=start, data=task_run_view(store, row, actor=args.actor))


TASK_TRANSITIONS: dict[str, set[str]] = {
    "open": {"running"},
    "running": {"reviewing", "blocked"},
    "blocked": {"running"},
    "reviewing": {"accepted", "rejected"},
    "accepted": {"closed"},
    "rejected": {"running", "closed"},
}

TASK_RUN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def task_project_id(store: Store, task: sqlite3.Row) -> str:
    return task["project_id"]


def changeset_task_id(store: Store, cs: sqlite3.Row) -> str | None:
    if cs["task_run_id"]:
        run = store.row("SELECT task_id FROM task_runs WHERE id = ?", (cs["task_run_id"],))
        if run and run["task_id"]:
            return run["task_id"]
    if cs["workspace_id"]:
        ws = store.row("SELECT task_id FROM workspaces WHERE id = ?", (cs["workspace_id"],))
        if ws and ws["task_id"]:
            return ws["task_id"]
    return None


def set_task_status(
    store: Store,
    task: sqlite3.Row,
    *,
    actor: str,
    status: str,
    event_kind: str,
    reason: str | None = None,
    accepted_changesets: list[str] | None = None,
    no_change: str | None = None,
) -> tuple[dict[str, Any], str]:
    require_object_access(store, task["id"], actor=actor, action="task.write")
    previous_status = task["status"]
    allowed = TASK_TRANSITIONS.get(previous_status, set())
    if status not in allowed:
        raise TriError(
            "task_lifecycle_blocked",
            "Task lifecycle transition is not allowed.",
            object=task["id"],
            status=previous_status,
            requested_status=status,
            allowed_transitions=sorted(allowed),
        )
    accepted_changesets = accepted_changesets or []
    if status == "accepted" and not accepted_changesets and not no_change:
        raise TriError("invalid_command", "Accepted tasks require --changeset or --no-change.", object=task["id"])
    for changeset_id in accepted_changesets:
        cs = store.get_changeset(changeset_id)
        if cs["project_id"] != task["project_id"]:
            raise TriError("invalid_command", "Accepted changeset must belong to the task project.", object=changeset_id)
        if changeset_task_id(store, cs) != task["id"]:
            raise TriError(
                "invalid_command",
                "Accepted changeset must belong to the accepted task.",
                object=changeset_id,
                task=task["id"],
            )
        require_object_access(store, cs["id"], actor=actor, action="changeset.read")
    lifecycle = {
        **loads(task["lifecycle_json"], {}),
        "previous_status": previous_status,
        "status": status,
        "transition": f"{previous_status}->{status}",
        "reason": reason,
        "actor": actor,
        "updated_at": utcnow(),
    }
    if accepted_changesets:
        lifecycle["accepted_changesets"] = accepted_changesets
    if no_change:
        lifecycle["no_change"] = no_change
    with store.conn:
        store.conn.execute("UPDATE tasks SET status = ?, lifecycle_json = ? WHERE id = ?", (status, dumps(lifecycle), task["id"]))
        evt = store.create_event(
            kind=event_kind,
            actor=actor,
            project_id=task["project_id"],
            task_id=task["id"],
            object_refs={"task": task["id"], "previous_status": previous_status, "status": status, "accepted_changesets": accepted_changesets, "no_change": bool(no_change)},
        )
    data = task_data(store, task["id"], actor=actor)
    data["previous_status"] = previous_status
    data["transition"] = f"{previous_status}->{status}"
    return data, evt


def cmd_task_block(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    task = store.get_task(args.task)
    data, evt = set_task_status(store, task, actor=args.actor, status="blocked", event_kind="task.blocked", reason=args.reason)
    return envelope("tri.task.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_task_resume(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    task = store.get_task(args.task)
    data, evt = set_task_status(store, task, actor=args.actor, status="running", event_kind="task.resumed", reason=args.reason)
    return envelope("tri.task.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_task_accept(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.changeset and args.no_change:
        raise TriError("invalid_command", "Use either --changeset or --no-change, not both.", object=args.task)
    task = store.get_task(args.task)
    data, evt = set_task_status(store, task, actor=args.actor, status="accepted", event_kind="task.accepted", accepted_changesets=args.changeset or [], no_change=args.no_change)
    return envelope("tri.task.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_task_reject(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    task = store.get_task(args.task)
    data, evt = set_task_status(store, task, actor=args.actor, status="rejected", event_kind="task.rejected", reason=args.reason)
    return envelope("tri.task.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_task_close(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    task = store.get_task(args.task)
    data, evt = set_task_status(store, task, actor=args.actor, status="closed", event_kind="task.closed", reason=args.reason)
    return envelope("tri.task.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_task_complete_run(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = get_task_run(store, args.run)
    if row["actor"] != args.actor and row["task_id"]:
        require_object_access(store, row["task_id"], actor=args.actor, action="task.write")
    if row["status"] in TASK_RUN_TERMINAL_STATUSES:
        raise TriError(
            "task_lifecycle_blocked",
            "Task run is terminal and cannot be completed again.",
            object=args.run,
            status=row["status"],
            requested_status=args.status,
        )
    if args.status == "completed" and not args.recommendation:
        raise TriError("invalid_command", "Completed task runs require a final recommendation.", object=args.run)
    with store.conn:
        store.conn.execute("UPDATE task_runs SET status = ?, final_recommendation = ? WHERE id = ?", (args.status, args.recommendation, args.run))
        if row["task_id"] and args.status == "completed":
            task = store.get_task(row["task_id"])
            if task["status"] in ("open", "running"):
                lifecycle = {
                    **loads(task["lifecycle_json"], {}),
                    "previous_status": task["status"],
                    "status": "reviewing",
                    "transition": f"{task['status']}->reviewing",
                    "actor": args.actor,
                    "updated_at": utcnow(),
                    "completed_run": args.run,
                    "recommendation": args.recommendation,
                }
                store.conn.execute("UPDATE tasks SET status = ?, lifecycle_json = ? WHERE id = ?", ("reviewing", dumps(lifecycle), row["task_id"]))
        evt = store.create_event(
            kind="task_run.completed" if args.status == "completed" else f"task_run.{args.status}",
            actor=args.actor,
            project_id=store.get_workspace(row["workspace_id"])["project_id"] if row["workspace_id"] else None,
            workspace_id=row["workspace_id"],
            task_id=row["task_id"],
            object_refs={"task_run": args.run, "previous_status": row["status"], "status": args.status, "recommendation": bool(args.recommendation)},
        )
    return envelope("tri.task_run.inspect", command, actor=args.actor, start=start, events=[evt], data=task_run_view(store, get_task_run(store, args.run), actor=args.actor))


def resolve_target_snapshot(store: Store, project_id: str, target_name: str) -> str:
    row = store.row("SELECT snapshot_id FROM targets WHERE project_id = ? AND name = ?", (project_id, target_name))
    if row is None:
        raise TriError("path_not_found", "Target not found.", object=target_name)
    return row["snapshot_id"]


def resolve_target_row(store: Store, project_id: str, target_name: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM targets WHERE project_id = ? AND name = ?", (project_id, target_name))
    if row is None:
        raise TriError("path_not_found", "Target not found.", object=target_name)
    return row


def create_import_snapshot_from_project_root(store: Store, project: sqlite3.Row, ref: str, *, actor: str, parent: str | None = None) -> dict[str, str]:
    root = Path(project["root_path"])
    entries = build_entries_from_git(store, root, ref) if (root / ".git").exists() else build_entries_from_dir(store, root, allow_symlinks=True, reject_escaping_symlinks=False)
    metadata: dict[str, Any] = {"source_path": str(root), "import_ref": ref}
    if (root / ".git").exists():
        metadata["git_commit"] = run_git(root, ["rev-parse", ref], text=True).strip()
        metadata["git_tree"] = run_git(root, ["rev-parse", f"{ref}^{{tree}}"], text=True).strip()
        metadata["git_author"] = git_author_metadata(root, ref)
    return store.create_snapshot(project["id"], entries, parents=[parent] if parent else [], source="imported", actor=actor, metadata=metadata)


def cmd_targets_refresh(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can refresh project targets.", object=args.target, action="target.refresh", required_grant="role:maintainer")
    project = store.get_project(args.project)
    row = store.row("SELECT * FROM targets WHERE project_id = ? AND name = ?", (project["id"], args.target))
    if row is None:
        raise TriError("path_not_found", "Target not found.", object=args.target)
    if args.expect_snapshot and args.expect_snapshot != row["snapshot_id"]:
        raise TriError(
            "stale_target_snapshot",
            "Target changed before refresh could start.",
            object=args.target,
            expected=args.expect_snapshot,
            actual=row["snapshot_id"],
            retryable=True,
        )
    ref = args.ref or args.target
    with store.conn:
        snapshot = create_import_snapshot_from_project_root(store, project, ref, actor=args.actor, parent=row["snapshot_id"])
        updated = store.conn.execute("UPDATE targets SET snapshot_id = ? WHERE id = ? AND snapshot_id = ?", (snapshot["id"], row["id"], row["snapshot_id"]))
        if updated.rowcount != 1:
            current = store.row("SELECT snapshot_id FROM targets WHERE id = ?", (row["id"],))
            raise TriError(
                "stale_target_snapshot",
                "Target changed while refresh was preparing the imported snapshot.",
                object=args.target,
                expected=row["snapshot_id"],
                actual=current["snapshot_id"] if current else None,
                retryable=True,
            )
        evt = store.create_event(
            kind="target.refreshed",
            actor=args.actor,
            project_id=project["id"],
            object_refs={"target": row["id"], "target_name": args.target, "snapshot": snapshot["id"], "previous_snapshot": row["snapshot_id"]},
            input_fingerprints={"snapshot": row["snapshot_id"]},
            output_fingerprints={"snapshot": snapshot["content_fingerprint"]},
        )
    metadata_row = store.row("SELECT metadata_json FROM snapshots WHERE id = ?", (snapshot["id"],))
    metadata = loads(metadata_row["metadata_json"], {}) if metadata_row else {}
    return envelope("tri.targets.refresh", command, actor=args.actor, start=start, events=[evt], data={"target": row["id"], "name": args.target, "snapshot": snapshot, "source": import_source_data(metadata, snapshot)})


def conflict_entry_kind(
    store: Store,
    base_entry: dict[str, Any] | None,
    head_entry: dict[str, Any] | None,
    target_entry: dict[str, Any] | None,
) -> str:
    if (head_entry is None) != (target_entry is None):
        return "delete_modify_conflict"
    if any(blob_is_binary(store, entry["blob"]) for entry in (base_entry, head_entry, target_entry) if entry):
        return "binary_conflict"
    return "file_text_conflict"


def merge_entries(
    store: Store,
    base: dict[str, dict[str, Any]],
    head: dict[str, dict[str, Any]],
    target: dict[str, dict[str, Any]],
    text_merged: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    merged: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    changed: list[str] = []
    for path in sorted(set(base) | set(head) | set(target)):
        base_entry = base.get(path)
        head_entry = head.get(path)
        target_entry = target.get(path)
        line_merged = None
        if head_entry == target_entry:
            chosen = head_entry
        elif head_entry == base_entry:
            chosen = target_entry
        elif target_entry == base_entry:
            chosen = head_entry
        elif (line_merged := line_merge_entry(store, base_entry, head_entry, target_entry)) is not None:
            chosen = line_merged
            if text_merged is not None:
                text_merged.append(path)
        else:
            conflicts.append(
                {
                    "kind": conflict_entry_kind(store, base_entry, head_entry, target_entry),
                    "path": path,
                    "base_blob": base_entry["blob"] if base_entry else None,
                    "workspace_blob": head_entry["blob"] if head_entry else None,
                    "target_blob": target_entry["blob"] if target_entry else None,
                }
            )
            chosen = head_entry
        if chosen is not None:
            merged[path] = chosen
        if chosen != head_entry:
            changed.append(path)
    return dict(sorted(merged.items())), conflicts, changed


def line_merge_entry(
    store: Store,
    base_entry: dict[str, Any] | None,
    head_entry: dict[str, Any] | None,
    target_entry: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge disjoint line edits to one text file; None means a real conflict."""
    if base_entry is None or head_entry is None or target_entry is None:
        return None
    modes = {entry.get("mode") for entry in (base_entry, head_entry, target_entry)}
    if not modes <= {"100644", "100755"}:
        return None
    if head_entry.get("mode") == base_entry.get("mode"):
        mode = target_entry.get("mode")
    elif target_entry.get("mode") == base_entry.get("mode") or target_entry.get("mode") == head_entry.get("mode"):
        mode = head_entry.get("mode")
    else:
        return None
    try:
        merged = merge_three_way(store.read_blob(base_entry["blob"]), store.read_blob(head_entry["blob"]), store.read_blob(target_entry["blob"]))
    except TriError:
        return None
    if merged is None:
        return None
    blob = store.put_blob(merged)
    return {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": mode}


def conflict_object_for_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {"exists": False, "blob": None, "content_fingerprint": None, "mode": None, "size": 0}
    return {
        "exists": True,
        "blob": entry["blob"],
        "content_fingerprint": f"sha256:{entry['hash']}",
        "mode": entry.get("mode"),
        "size": entry.get("size"),
    }


def snapshot_source_for_conflict(store: Store, snapshot_id: str, *, side: str) -> dict[str, Any]:
    row = store.row("SELECT id, source, created_by, created_at, metadata_json FROM snapshots WHERE id = ?", (snapshot_id,))
    if row is None:
        raise TriError("path_not_found", "Snapshot not found.", object=snapshot_id)
    metadata = loads(row["metadata_json"], {})
    data = {
        "side": side,
        "snapshot": row["id"],
        "source": row["source"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
    if metadata.get("git_commit"):
        data["git_commit"] = metadata["git_commit"]
    git_author = metadata.get("git_author")
    if isinstance(git_author, dict):
        data["git_author_actor"] = git_author.get("actor")
    return data


def workspace_operations_for_conflict_paths(store: Store, workspace_id: str, paths: set[str]) -> dict[str, list[dict[str, Any]]]:
    operations: dict[str, list[dict[str, Any]]] = {path: [] for path in paths}
    for row in store.rows("SELECT * FROM operations WHERE workspace_id = ? ORDER BY created_at, rowid", (workspace_id,)):
        operation_paths = loads(row["path_set_json"], [])
        touched = sorted(path for path in paths if path in operation_paths)
        if not touched:
            continue
        operation = {
            "id": row["id"],
            "actor": row["actor"],
            "kind": row["kind"],
            "input_snapshot": row["input_snapshot_id"],
            "output_snapshot": row["output_snapshot_id"],
            "paths": operation_paths,
            "created_at": row["created_at"],
        }
        for path in touched:
            operations[path].append(operation)
    return operations


def proposed_conflict_resolutions(conflict_id: str, conflict: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "strategy": "ours",
            "source": "workspace",
            "object": conflict.get("workspace_blob"),
            "command": ["tri", "conflicts", "resolve", conflict_id, "--strategy", "ours"],
        },
        {
            "strategy": "theirs",
            "source": "target",
            "object": conflict.get("target_blob"),
            "command": ["tri", "conflicts", "resolve", conflict_id, "--strategy", "theirs"],
        },
        {
            "strategy": "base",
            "source": "base",
            "object": conflict.get("base_blob"),
            "command": ["tri", "conflicts", "resolve", conflict_id, "--strategy", "base"],
        },
    ]


def enrich_workspace_conflicts(
    store: Store,
    ws: sqlite3.Row,
    *,
    conflict_id: str,
    target_snapshot: str,
    conflicts: list[dict[str, Any]],
    actor: str,
) -> list[dict[str, Any]]:
    base_entries = store.load_snapshot_tree(ws["base_snapshot_id"])
    head_entries = store.load_snapshot_tree(ws["head_snapshot_id"])
    target_entries = store.load_snapshot_tree(target_snapshot)
    operations_by_path = workspace_operations_for_conflict_paths(store, ws["id"], {conflict["path"] for conflict in conflicts})
    sources = [
        snapshot_source_for_conflict(store, ws["base_snapshot_id"], side="base"),
        snapshot_source_for_conflict(store, ws["head_snapshot_id"], side="workspace"),
        snapshot_source_for_conflict(store, target_snapshot, side="target"),
    ]
    enriched = []
    for conflict in conflicts:
        path = conflict["path"]
        _, path_redaction = path_visibility(path, effective_hidden_patterns(store, ws["project_id"], loads(ws["hidden_paths_json"], []), actor=actor))
        involved_operations = operations_by_path.get(path, [])
        actors = {actor, ws["owner_actor"]}
        actors.update(operation["actor"] for operation in involved_operations)
        for source in sources:
            actors.add(source["created_by"])
            if source.get("git_author_actor"):
                actors.add(source["git_author_actor"])
        kind = conflict["kind"]
        if path_redaction == "secret_like_path":
            kind = "secret_export_conflict"
        elif path_redaction == "hidden_path":
            kind = "path_visibility_conflict"
        item = {
            **conflict,
            "kind": kind,
            "conflicting_objects": {
                "base": conflict_object_for_entry(base_entries.get(path)),
                "workspace": conflict_object_for_entry(head_entries.get(path)),
                "target": conflict_object_for_entry(target_entries.get(path)),
            },
            "involved_actors": sorted(actor for actor in actors if actor),
            "involved_operations": involved_operations,
            "source_snapshots": sources,
            "proposed_resolutions": proposed_conflict_resolutions(conflict_id, conflict),
            "required_policy_checks": [
                {"action": "workspace.read", "object": ws["id"], "reason": "inspect conflict"},
                {"action": "workspace.write", "object": ws["id"], "reason": "resolve conflict"},
            ],
        }
        enriched.append(item)
    return enriched


def conflict_policy_view(conflict: dict[str, Any], hidden_patterns: list[str]) -> dict[str, Any]:
    path = conflict.get("path")
    if isinstance(path, str):
        visible, reason = path_visibility(path, hidden_patterns)
        if not visible:
            return {
                "kind": conflict.get("kind"),
                "path": redacted_path_ref(path),
                "redacted": True,
                "redaction": reason,
                "proposed_resolutions": [
                    {"strategy": item.get("strategy"), "command": item.get("command")}
                    for item in conflict.get("proposed_resolutions", [])
                ],
                "required_policy_checks": conflict.get("required_policy_checks", []),
            }
    return conflict


def workspace_conflict_data(store: Store, row: sqlite3.Row) -> dict[str, Any]:
    ws = store.get_workspace(row["workspace_id"])
    hidden_patterns = loads(ws["hidden_paths_json"], [])
    return {
        "id": row["id"],
        "project": row["project_id"],
        "workspace": row["workspace_id"],
        "base_snapshot": row["base_snapshot_id"],
        "head_snapshot": row["head_snapshot_id"],
        "target_snapshot": row["target_snapshot_id"],
        "target_ref": row["target_ref"],
        "conflicts": [conflict_policy_view(conflict, hidden_patterns) for conflict in loads(row["conflicts_json"], [])],
        "status": row["status"],
        "resolution": loads(row["resolution_json"], {}),
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def create_workspace_conflict(
    store: Store,
    ws: sqlite3.Row,
    *,
    target_ref: str,
    target_snapshot: str,
    conflicts: list[dict[str, Any]],
    actor: str,
) -> tuple[str, str]:
    conflict_id = new_id("conflict")
    conflicts = enrich_workspace_conflicts(store, ws, conflict_id=conflict_id, target_snapshot=target_snapshot, conflicts=conflicts, actor=actor)
    with store.conn:
        current = store.get_workspace(ws["id"])
        if current["head_snapshot_id"] != ws["head_snapshot_id"]:
            raise_stale_workspace_head(store, ws["id"], ws["head_snapshot_id"], "Workspace head changed before conflict could be recorded.")
        store.conn.execute(
            """
            INSERT INTO workspace_conflicts (
              id, project_id, workspace_id, base_snapshot_id, head_snapshot_id,
              target_snapshot_id, target_ref, conflicts_json, status,
              resolution_json, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict_id,
                ws["project_id"],
                ws["id"],
                ws["base_snapshot_id"],
                ws["head_snapshot_id"],
                target_snapshot,
                target_ref,
                dumps(conflicts),
                "open",
                dumps({}),
                utcnow(),
                None,
            ),
        )
        evt = store.create_event(
            kind="workspace.conflict_required",
            actor=actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"conflict": conflict_id, "target_snapshot": target_snapshot, "paths": [item["path"] for item in conflicts]},
            input_fingerprints={"base_snapshot": ws["base_snapshot_id"], "head_snapshot": ws["head_snapshot_id"]},
            output_fingerprints={"target_snapshot": target_snapshot},
        )
    return conflict_id, evt


def cmd_workspace_create(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    base_snapshot = resolve_target_snapshot(store, project["id"], args.from_ref)
    advisory = resolve_advisory(store, project["id"], args.advisory) if getattr(args, "advisory", None) else None
    if advisory:
        require_object_access(store, advisory["id"], actor=args.actor, action="advisory.read")
    task_id = args.task if args.task and args.task.startswith("task_") else None
    task_scope = actor_task_scope(args.actor)
    if task_scope is not None and task_id != task_scope:
        raise TriError("policy_denied", "Task-scoped actors can only create workspaces for their task.", object=args.project, task_scope=task_scope, task=task_id, required_grant=f"task:{task_scope}")
    hidden_paths = sorted(set(normalize_hidden_patterns(args.hide_path) + package_hidden_patterns(store, project["id"], args.hide_package, actor=args.actor)))
    visibility = args.visibility or (["embargoed"] if advisory else ["private"])
    with store.conn:
        if args.task and task_id is None:
            task_id = new_id("task")
            store.conn.execute(
                "INSERT INTO tasks (id, project_id, title, body, requester, acceptance_criteria_json, required_checks_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, project["id"], args.task, "", args.actor, dumps([]), dumps([]), "running", utcnow()),
            )
        elif task_id is not None:
            task = store.get_task(task_id)
            if task["project_id"] != project["id"]:
                raise TriError("invalid_command", "Task must belong to the workspace project.", object=task_id)
            task_access = "task.run" if task["status"] in ("open", "running") else "task.write"
            require_object_access(store, task_id, actor=args.actor, action=task_access)
            if task["status"] not in ("open", "running", "blocked", "rejected"):
                raise TriError(
                    "task_lifecycle_blocked",
                    "Task cannot accept new workspace attempts in its current state.",
                    object=task_id,
                    status=task["status"],
                    allowed_statuses=["open", "running", "blocked", "rejected"],
                )
            if task["status"] in ("open", "blocked", "rejected"):
                lifecycle = {
                    **loads(task["lifecycle_json"], {}),
                    "previous_status": task["status"],
                    "status": "running",
                    "transition": f"{task['status']}->running",
                    "actor": args.actor,
                    "updated_at": utcnow(),
                }
                store.conn.execute("UPDATE tasks SET status = ?, lifecycle_json = ? WHERE id = ?", ("running", dumps(lifecycle), task_id))
        workspace_id = new_id("ws")
        now = utcnow()
        store.conn.execute(
            """
            INSERT INTO workspaces (
              id, project_id, base_snapshot_id, head_snapshot_id, owner_actor,
              task_id, status, visibility_json, hidden_paths_json, advisory_id, materialized_path, materialized_snapshot_id,
              ttl, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                project["id"],
                base_snapshot,
                base_snapshot,
                args.actor,
                task_id,
                "active",
                dumps(visibility),
                dumps(hidden_paths),
                advisory["id"] if advisory else None,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        run_id = new_id("run")
        store.conn.execute(
            "INSERT INTO task_runs (id, task_id, actor, workspace_id, status, final_recommendation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, args.actor, workspace_id, "started", None, now),
        )
        evt = store.create_event(
            kind="workspace.created",
            actor=args.actor,
            project_id=project["id"],
            workspace_id=workspace_id,
            task_id=task_id,
            object_refs={"workspace": workspace_id, "base_snapshot": base_snapshot, "task_run": run_id, "advisory": advisory["id"] if advisory else None, "hidden_packages": args.hide_package or []},
        )
    return envelope(
        "tri.workspace.create",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={
            "id": workspace_id,
            "project": project["id"],
            "base_snapshot": base_snapshot,
            "head_snapshot": base_snapshot,
            "task": task_id,
            "task_run": run_id,
            "status": "active",
            "visibility": visibility,
            "hidden_paths": hidden_paths,
            "advisory": advisory["id"] if advisory else None,
        },
    )


def workspace_data(store: Store, workspace_id: str, *, actor: str | None = None) -> dict[str, Any]:
    ws = store.get_workspace(workspace_id)
    checks = [row["id"] for row in store.rows("SELECT id FROM check_runs WHERE workspace_id = ? ORDER BY created_at", (workspace_id,))]
    changesets = [row["id"] for row in store.rows("SELECT id FROM changesets WHERE workspace_id = ? ORDER BY created_at", (workspace_id,))]
    redacted_checks = 0
    redacted_changesets = 0
    if actor is not None:
        visible_checks = []
        for check_id in checks:
            decision = check_access_decision(store, check_data(store, check_id), actor=actor)
            if decision["allowed"]:
                visible_checks.append(check_id)
            else:
                redacted_checks += 1
        checks = visible_checks

        visible_changesets = []
        for changeset_id in changesets:
            decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
            if decision["allowed"]:
                visible_changesets.append(changeset_id)
            else:
                redacted_changesets += 1
        changesets = visible_changesets
    last_op = store.row("SELECT id FROM operations WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
    task_run = latest_workspace_task_run(store, workspace_id)
    materializations = [
        materialization_lease_data(row)
        for row in store.rows("SELECT * FROM materialization_leases WHERE workspace_id = ? ORDER BY created_at, rowid", (workspace_id,))
    ]
    return {
        "id": ws["id"],
        "project": ws["project_id"],
        "base_snapshot": ws["base_snapshot_id"],
        "head_snapshot": ws["head_snapshot_id"],
        "owner": ws["owner_actor"],
        "task": ws["task_id"],
        "task_run": task_run["id"] if task_run else None,
        "status": ws["status"],
        "visibility": loads(ws["visibility_json"], []),
        "hidden_paths": loads(ws["hidden_paths_json"], []),
        "advisory": ws["advisory_id"],
        "last_operation": last_op["id"] if last_op else None,
        "materializations": materializations,
        "checks": checks,
        "redacted_checks": redacted_checks,
        "changesets": changesets,
        "redacted_changesets": redacted_changesets,
    }


def cmd_workspace_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_object_access(store, args.workspace, actor=args.actor, action="workspace.read")
    return envelope("tri.workspace.inspect", command, actor=args.actor, start=start, data=workspace_data(store, args.workspace, actor=args.actor))


def cmd_workspace_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.project:
        project = store.get_project(args.project)
        rows = store.rows("SELECT id FROM workspaces WHERE project_id = ? ORDER BY created_at", (project["id"],))
        project_id = project["id"]
    else:
        rows = store.rows("SELECT id FROM workspaces ORDER BY created_at")
        project_id = None
    workspaces = []
    redacted = 0
    for row in rows:
        decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="workspace.read")
        if decision["allowed"]:
            workspaces.append(workspace_data(store, row["id"], actor=args.actor))
        else:
            redacted += 1
    return envelope("tri.workspace.list", command, actor=args.actor, start=start, data={"project": project_id, "workspaces": workspaces, "redacted": redacted})


def cmd_workspace_snapshot(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can be snapshotted.", object=ws["id"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    before = ws["head_snapshot_id"]
    if getattr(args, "expect_head", None) and args.expect_head != before:
        raise_stale_workspace_head(store, ws["id"], args.expect_head, "Workspace head changed before checkpoint could start.")
    entries = store.load_snapshot_tree(before)
    with store.conn:
        snapshot = store.create_snapshot(
            ws["project_id"],
            entries,
            parents=[before],
            source="workspace",
            actor=args.actor,
            metadata={"workspace": ws["id"], "operation": "checkpoint"},
        )
        op_id = new_id("op")
        store.conn.execute(
            "INSERT INTO operations (id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id, path_set_json, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, ws["id"], args.actor, "checkpoint", before, snapshot["id"], dumps([]), dumps({}), utcnow()),
        )
        materialized_snapshot = snapshot["id"] if ws["materialized_snapshot_id"] == before else ws["materialized_snapshot_id"]
        updated = store.conn.execute(
            """
            UPDATE workspaces
            SET head_snapshot_id = ?, materialized_snapshot_id = ?, updated_at = ?
            WHERE id = ? AND head_snapshot_id = ?
            """,
            (snapshot["id"], materialized_snapshot, utcnow(), ws["id"], before),
        )
        if updated.rowcount != 1:
            raise_stale_workspace_head(store, ws["id"], before, "Workspace head changed before checkpoint could commit.")
        store.conn.execute(
            "UPDATE materialization_leases SET last_sync_snapshot_id = ? WHERE workspace_id = ? AND last_sync_snapshot_id = ? AND status = ?",
            (snapshot["id"], ws["id"], before, "active"),
        )
        evt = store.create_event(
            kind="workspace.checkpoint",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"operation": op_id, "snapshot": snapshot["id"]},
            input_fingerprints={"snapshot": before},
            output_fingerprints={"snapshot": snapshot["id"]},
        )
    return envelope("tri.workspace.snapshot", command, actor=args.actor, start=start, events=[evt], data={"workspace": ws["id"], "operation": op_id, "snapshot": snapshot["id"]})


WORKSPACE_TRANSITIONS: dict[str, set[str]] = {
    "active": {"paused", "archived", "discarded", "promoted"},
    "paused": {"active", "archived", "discarded"},
    "promoted": {"archived"},
}


def set_workspace_status(store: Store, args: argparse.Namespace, command: list[str], start: float, *, status: str, event_kind: str) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    previous_status = ws["status"]
    allowed = WORKSPACE_TRANSITIONS.get(previous_status, set())
    if status not in allowed:
        raise TriError(
            "workspace_lifecycle_blocked",
            "Workspace lifecycle transition is not allowed.",
            object=ws["id"],
            status=previous_status,
            requested_status=status,
            allowed_transitions=sorted(allowed),
        )
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    with store.conn:
        store.conn.execute("UPDATE workspaces SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), ws["id"]))
        evt = store.create_event(
            kind=event_kind,
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"workspace": ws["id"], "previous_status": previous_status, "status": status},
        )
    reaped = None
    if status in ("discarded", "archived"):
        reaped = reap_leases(store, actor=args.actor, workspace_id=ws["id"], reason=f"workspace_{status}")
    data = workspace_data(store, ws["id"], actor=args.actor)
    data["previous_status"] = previous_status
    data["transition"] = f"{previous_status}->{status}"
    if reaped is not None:
        data["reaped_leases"] = reaped
    return envelope("tri.workspace.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_workspace_pause(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return set_workspace_status(store, args, command, start, status="paused", event_kind="workspace.paused")


def cmd_workspace_resume(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return set_workspace_status(store, args, command, start, status="active", event_kind="workspace.resumed")


def cmd_workspace_discard(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return set_workspace_status(store, args, command, start, status="discarded", event_kind="workspace.discarded")


def cmd_workspace_archive(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return set_workspace_status(store, args, command, start, status="archived", event_kind="workspace.archived")


def cmd_workspace_mount(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    read_only = ws["status"] == "archived"
    if ws["status"] not in ("active", "archived"):
        raise TriError("policy_denied", "Only active or archived workspaces can be materialized.", object=ws["id"], status=ws["status"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
    entries = store.load_snapshot_tree(ws["head_snapshot_id"])
    requested_root = Path(args.path)
    if requested_root.is_symlink():
        raise TriError("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(requested_root))
    root = requested_root.resolve()
    ignored_paths = normalize_hidden_patterns(args.ignore_path)
    if args.ttl_seconds is not None and args.ttl_seconds <= 0:
        raise TriError("invalid_command", "Materialization lease TTL must be positive.", object=ws["id"])
    if args.no_ttl and args.ttl_seconds is not None:
        raise TriError("invalid_command", "Pass either --ttl-seconds or --no-ttl, not both.", object=ws["id"])
    ttl_seconds = None if args.no_ttl else (args.ttl_seconds or DEFAULT_MOUNT_TTL_SECONDS)
    expires_at = lease_expiry(ttl_seconds) if ttl_seconds else None
    claim_mount_path(store, root, actor=args.actor)
    lease_id = new_id("lease")
    sparse = normalize_path_patterns(args.paths)
    env_plan = EnvPlan() if args.no_env else plan_environments(store, ws["project_id"], entries, actor=args.actor, workspace=ws, purpose="mount", env_link=args.env_link)
    materialization = materialize_entries(store, sparse_entries(entries, sparse), root, base_snapshot=None if sparse else ws["base_snapshot_id"], attachments=env_plan.attachments)
    env_plan.finalize(materialization)
    if read_only:
        make_materialization_read_only(root)
        materialization.refresh_baseline()
    baseline_path = store.leases_dir / f"{lease_id}.baseline.json"
    materialization.save_baseline(baseline_path)
    with store.conn:
        store.conn.execute(
            "UPDATE workspaces SET materialized_path = ?, materialized_snapshot_id = ?, updated_at = ? WHERE id = ?",
            (str(root), ws["head_snapshot_id"], utcnow(), ws["id"]),
        )
        store.conn.execute(
            """
            INSERT INTO materialization_leases (
              id, workspace_id, actor, path, mounted_snapshot_id, last_sync_snapshot_id,
              ignored_paths_json, generated_artifacts_json, status, expires_at, created_at, revoked_at,
              backend, degraded, baseline_path, ttl_seconds, env_keys_json, root_identity, sparse_paths_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id,
                ws["id"],
                args.actor,
                str(root),
                ws["head_snapshot_id"],
                ws["head_snapshot_id"],
                dumps(ignored_paths),
                dumps([]),
                "read_only" if read_only else "active",
                expires_at,
                utcnow(),
                None,
                materialization.backend,
                1 if materialization.degraded else 0,
                str(baseline_path),
                ttl_seconds,
                dumps([record["key"] for record in env_plan.records if record.get("key")]),
                directory_identity(root),
                dumps(sparse) if sparse else None,
            ),
        )
        evt = store.create_event(
            kind="workspace.mounted",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"path": str(root), "snapshot": ws["head_snapshot_id"], "lease": lease_id, "read_only": read_only},
        )
    return envelope(
        "tri.workspace.mount",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={
            "workspace": ws["id"],
            "path": str(root),
            "snapshot": ws["head_snapshot_id"],
            "lease": lease_id,
            "read_only": read_only,
            "materialization": materialization_lease_data(store.get_materialization_lease(lease_id)),
            "environments": env_plan.records,
            "notes": materialization.notes,
        },
    )


def cmd_workspace_leases(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
    rows = store.rows("SELECT * FROM materialization_leases WHERE workspace_id = ? ORDER BY created_at, rowid", (ws["id"],))
    return envelope("tri.workspace.materialization_leases", command, actor=args.actor, start=start, data={"workspace": ws["id"], "leases": [materialization_lease_data(row) for row in rows]})


def cmd_workspace_lease_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    lease = store.get_materialization_lease(args.lease)
    require_object_access(store, lease["workspace_id"], actor=args.actor, action="workspace.read")
    return envelope("tri.workspace.materialization_lease", command, actor=args.actor, start=start, data=materialization_lease_data(lease))


def cmd_leases_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    kind, row = resolve_lease_for_inspect(store, args.lease)
    if kind == "materialization":
        require_object_access(store, row["workspace_id"], actor=args.actor, action="workspace.read")
        data = lease_inspect_data(kind, materialization_lease_data(row))
    elif kind == "directory":
        if row["workspace_id"]:
            require_object_access(store, row["workspace_id"], actor=args.actor, action="workspace.read")
        data = {"id": row["id"], "kind": "directory", "schema": "tri.disk_lease.inspect", "lease": disk_lease_data(row)}
    else:
        secret_lease_inspect_access(store, row, actor=args.actor)
        data = lease_inspect_data(kind, secret_lease_data(row))
    return envelope("tri.lease.inspect", command, actor=args.actor, start=start, data=data)


def cmd_workspace_lease_revoke(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    lease = store.get_materialization_lease(args.lease)
    ws = store.get_workspace(lease["workspace_id"])
    if lease["actor"] != args.actor:
        require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    status = "revoked" if lease["status"] != "revoked" else lease["status"]
    with store.conn:
        store.conn.execute("UPDATE materialization_leases SET status = ?, revoked_at = ? WHERE id = ?", (status, utcnow(), lease["id"]))
        evt = store.create_event(
            kind="workspace.materialization_lease_revoked",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"workspace": ws["id"], "lease": lease["id"], "path": lease["path"]},
        )
    return envelope("tri.workspace.materialization_lease", command, actor=args.actor, start=start, events=[evt], data=materialization_lease_data(store.get_materialization_lease(args.lease)))


DEFAULT_MOUNT_TTL_SECONDS = 86400
AUTO_REAP_INTERVAL_SECONDS = 600
SERVE_REAP_INTERVAL_SECONDS = 300
AUTO_REAP_LIMIT = 20
REAPER_ACTOR = "service:tri-reaper"
GENERATED_CONFIG_PREFIX = ".tri/generated-config"
REAPABLE_MOUNT_STATUSES = ("active", "read_only", "expired", "revoked", "expired_needs_attention")


def lease_expiry(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def store_meta_get(store: Store, key: str) -> str | None:
    row = store.row("SELECT value FROM store_meta WHERE key = ?", (key,))
    return row["value"] if row else None


def store_meta_set(store: Store, key: str, value: str) -> None:
    with store.conn:
        store.conn.execute("INSERT INTO store_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def command_sandbox(
    store: Store,
    run_dir: Path,
    *,
    purpose: str,
    requested: str | None = None,
    network: bool | None = None,
    timeout: float | None = None,
) -> sandbox.Sandbox | None:
    """The sandbox for a command Tri runs from workspace content, per `TRI_RUN_SANDBOX`
    (`tri serve --run-sandbox`) or an explicit request; None when sandboxing is off."""
    mode = sandbox.resolve_mode(requested)
    if network is None:
        network = os.environ.get("TRI_RUN_SANDBOX_NETWORK", "0") == "1"
    try:
        return sandbox.build(
            mode,
            run_dir=run_dir,
            hidden=[store.home],
            readable=[store.home / "envs"],
            network=network,
            limits=sandbox.Limits.from_env(timeout_seconds=timeout),
        )
    except sandbox.SandboxUnavailable as exc:
        raise TriError(
            "sandbox_unavailable",
            "Sandboxed runs are required here, but this host cannot isolate commands.",
            reason=str(exc),
            purpose=purpose,
            hint="Install bubblewrap (Linux) or run on macOS; or set --run-sandbox auto to accept limits without isolation.",
        ) from None


def sandbox_env(box: sandbox.Sandbox | None, run_dir: Path, base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    if box is None:
        env = dict(base)
        env.update(extra)
        return env
    return sandbox.scrubbed_env(run_dir, extra)


def trash_dir(store: Store) -> Path:
    return store.home / "trash"


def discard_tree(store: Store, path: Path) -> None:
    """Remove a released directory without making the caller wait for it.

    Deleting a 50,000-file run directory costs seconds of unlink calls, so the
    directory is renamed into the store's trash (atomic, same volume) and deleted
    by a detached process. The reaper and `tri gc` empty whatever is left.
    `TRI_DEFER_CLEANUP=0` deletes inline."""
    path = Path(path)
    if not path.exists():
        return
    if os.environ.get("TRI_DEFER_CLEANUP", "1") == "0":
        remove_tree(path)
        return
    trash = trash_dir(store)
    try:
        trash.mkdir(parents=True, exist_ok=True)
        doomed = trash / f"{int(time.time())}-{path.name}-{uuid.uuid4().hex[:8]}"
        os.rename(path, doomed)
    except OSError:
        remove_tree(path)
        return
    spawn_tree_remover(doomed)


def spawn_tree_remover(path: Path) -> None:
    code = "import sys\nfrom pathlib import Path\nfrom tri.materialize import remove_tree\nremove_tree(Path(sys.argv[1]))\n"
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        remover = subprocess.Popen([sys.executable, "-c", code, str(path)], env=env, **kwargs)
    except OSError:
        remove_tree(path)
        return
    # Reap it so a long-lived `tri serve` does not collect zombies; a CLI exit leaves it running.
    threading.Thread(target=remover.wait, daemon=True).start()


def empty_trash(store: Store, *, min_age_seconds: float = 600.0) -> int:
    """Delete trash entries left by interrupted background removals. Entries
    younger than ``min_age_seconds`` are left to the remover that owns them."""
    trash = trash_dir(store)
    if not trash.is_dir():
        return 0
    removed = 0
    now = time.time()
    for child in list(trash.iterdir()):
        stamp = child.name.split("-", 1)[0]
        if stamp.isdigit() and now - int(stamp) < min_age_seconds:
            continue
        try:
            remove_tree(child)
            removed += 1
        except OSError:
            pass
    return removed


class RunLease:
    """A directory Tri owns while a command runs, with a holder lock that the
    reaper probes to tell a live run from a crashed one."""

    def __init__(self, store: Store, *, kind: str, key: str, workspace_id: str | None, before_snapshot: str | None) -> None:
        self.store = store
        self.id = new_id("lease")
        self.kind = kind
        self.dir = store.runs / key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.work = self.dir / "work"
        self.baseline_path = self.dir / "baseline.json"
        self.holder = FileLock(self.dir / ".holder.lock")
        self.holder.acquire(exclusive=True)
        self.command_finished = False
        with store.conn:
            store.conn.execute(
                """
                INSERT INTO disk_leases (id, kind, path, workspace_id, check_id, before_snapshot_id, baseline_path, holder_lock, backend, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?)
                """,
                (self.id, kind, str(self.work), workspace_id, key, before_snapshot, str(self.baseline_path), str(self.dir / ".holder.lock"), utcnow()),
            )

    def record_envs(self, plan: "EnvPlan") -> None:
        keys = [record["key"] for record in plan.records if record.get("key")]
        if keys:
            with self.store.conn:
                self.store.conn.execute("UPDATE disk_leases SET env_keys_json = ? WHERE id = ?", (dumps(keys), self.id))

    def record_sparse(self, patterns: list[str]) -> None:
        if patterns:
            with self.store.conn:
                self.store.conn.execute("UPDATE disk_leases SET sparse_paths_json = ? WHERE id = ?", (dumps(patterns), self.id))

    def record_materialization(self, materialization: Materialization) -> None:
        materialization.save_baseline(self.baseline_path)
        with self.store.conn:
            self.store.conn.execute("UPDATE disk_leases SET backend = ? WHERE id = ?", (materialization.backend, self.id))

    def release(self) -> None:
        # Mark released while still holding the holder lock, so a reaper that
        # sees the lock drop can never mistake a cleanup in progress for a crash.
        with self.store.conn:
            self.store.conn.execute("UPDATE disk_leases SET status = 'released', released_at = ? WHERE id = ? AND status = 'active'", (utcnow(), self.id))
        # The reaper only claims 'active' leases, so the lock can drop now; it must
        # close before removal because Windows cannot delete an open file.
        self.holder.close()
        discard_tree(self.store, self.dir)

    def abandon(self) -> None:
        """Keep the directory for the reaper to salvage (the command ran but its
        changes were not imported); only drop the holder lock."""
        self.holder.close()

    def finish(self, failed: bool) -> None:
        if failed and self.command_finished:
            self.abandon()
        else:
            self.release()


def disk_lease_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "path": row["path"],
        "workspace": row["workspace_id"],
        "check": row["check_id"],
        "before_snapshot": row["before_snapshot_id"],
        "backend": row["backend"],
        "status": row["status"],
        "salvage": row["salvage_id"],
        "created_at": row["created_at"],
        "released_at": row["released_at"],
    }


def salvage_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace": row["workspace_id"],
        "lease": row["lease_id"],
        "kind": row["kind"],
        "base_snapshot": row["base_snapshot_id"],
        "snapshot": row["snapshot_id"],
        "paths": loads(row["paths_json"], []),
        "deleted_ignored": loads(row["deleted_ignored_json"], {"count": 0, "paths": []}),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "adopted_at": row["adopted_at"],
        "adopted_by": row["adopted_by"],
    }


def create_salvage(
    store: Store,
    ws: sqlite3.Row,
    *,
    lease_id: str,
    kind: str,
    base_snapshot: str,
    entries: dict[str, dict[str, Any]],
    paths: list[str],
    deleted_ignored: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    salvage_id = new_id("salvage")
    with store.conn:
        snapshot = store.create_snapshot(
            ws["project_id"],
            entries,
            parents=[base_snapshot],
            source="workspace",
            actor=actor,
            metadata={"workspace": ws["id"], "operation": "salvage", "lease": lease_id, "salvage": salvage_id},
        )
        store.conn.execute(
            """
            INSERT INTO salvages (id, workspace_id, lease_id, kind, base_snapshot_id, snapshot_id, paths_json, deleted_ignored_json, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (salvage_id, ws["id"], lease_id, kind, base_snapshot, snapshot["id"], dumps(paths), dumps(deleted_ignored), actor, utcnow()),
        )
        store.create_event(
            kind="lease.salvaged",
            actor=actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"salvage": salvage_id, "lease": lease_id, "snapshot": snapshot["id"], "paths": paths},
            input_fingerprints={"snapshot": base_snapshot},
            output_fingerprints={"snapshot": snapshot["id"]},
        )
    return salvage_data(store.row("SELECT * FROM salvages WHERE id = ?", (salvage_id,)))


DERIVED_DIR_NAMES = frozenset(
    {
        "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".turbo", ".gradle", ".tox", ".parcel-cache", ".cache", "coverage",
    }
)
JUNK_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def derived_dir_names() -> frozenset[str]:
    extra = {name.strip() for name in os.environ.get("TRI_DERIVED_DIRS", "").split(",") if name.strip()}
    return DERIVED_DIR_NAMES | extra


def classify_uncaptured(
    root: Path,
    materialization: Materialization,
    before_entries: dict[str, dict[str, Any]],
    captured: set[str],
    rules: IgnoreRules,
    lease_ignored: list[str],
) -> dict[str, Any]:
    """Sort files a reap would not capture into "deletable" and "needs a human".

    Deletable: everything inside ignored dependency/build directories
    (``node_modules``, ``.venv``, ``target``...) and OS junk files. Needs a human:
    secret-like files, other ignored files, and ``.git`` directories the user made.
    """
    derived = derived_dir_names()
    skip = set(materialization.skip_prefixes)
    secret_like: list[str] = []
    user_git: list[str] = []
    ignored_kept: set[str] = set()
    collapsed: set[str] = set()
    count = 0

    def ignored_dir(rel: str) -> bool:
        return rules.dir_ignored(rel) or path_matches_hidden(rel, lease_ignored)

    def walk(directory: str, rel: str, derived_root: str | None, ignored_root: str | None) -> None:
        nonlocal count
        try:
            it = os.scandir(directory)
        except OSError:
            return
        with it:
            for entry in it:
                child = f"{rel}/{entry.name}" if rel else entry.name
                if child in skip or child == GENERATED_CONFIG_PREFIX or child.startswith(GENERATED_CONFIG_PREFIX + "/"):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if derived_root is not None:
                    if is_dir:
                        walk(entry.path, child, derived_root, ignored_root)
                    else:
                        count += 1
                        collapsed.add(derived_root + "/")
                    continue
                if entry.name == ".git":
                    user_git.append(child)
                    continue
                if is_dir:
                    if child not in before_entries and ignored_dir(child):
                        if entry.name in derived:
                            walk(entry.path, child, child, ignored_root)
                        else:
                            walk(entry.path, child, None, ignored_root or child)
                        continue
                    walk(entry.path, child, None, ignored_root)
                    continue
                if child in captured or (child in before_entries and not path_matches_hidden(child, lease_ignored)):
                    continue
                if entry.name in JUNK_FILE_NAMES:
                    count += 1
                    collapsed.add(child)
                elif is_secret_like_path(child):
                    secret_like.append(child)
                else:
                    ignored_kept.add(ignored_root + "/" if ignored_root else child)

    walk(str(root), "", None, None)
    ordered = sorted(collapsed)
    return {
        "secret_like_paths": sorted(secret_like),
        "user_git_dirs": sorted(user_git),
        "ignored_paths": sorted(ignored_kept),
        "deleted_ignored": {"count": count, "paths": ordered[:200], "truncated": len(ordered) > 200},
    }


def _lease_update(store: Store, table: str, lease_id: str, **fields: Any) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    with store.conn:
        store.conn.execute(f"UPDATE {table} SET {columns} WHERE id = ?", (*fields.values(), lease_id))


def directory_identity(path: Path) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_dev}:{st.st_ino}"


def reap_mount_lease(store: Store, lease: sqlite3.Row, *, actor: str, force: bool = False, reason: str = "expired") -> dict[str, Any] | None:
    with store.conn:
        claimed = store.conn.execute(
            "UPDATE materialization_leases SET status = 'reaping' WHERE id = ? AND status = ? AND reaped_at IS NULL",
            (lease["id"], lease["status"]),
        ).rowcount
    if claimed != 1:
        return None
    try:
        return _reap_claimed_mount_lease(store, lease, actor=actor, force=force, reason=reason)
    except BaseException:
        with store.conn:
            store.conn.execute("UPDATE materialization_leases SET status = ? WHERE id = ? AND status = 'reaping'", (lease["status"], lease["id"]))
        raise


def _reap_claimed_mount_lease(store: Store, lease: sqlite3.Row, *, actor: str, force: bool, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {"lease": lease["id"], "kind": "mount", "path": lease["path"], "workspace": lease["workspace_id"], "reason": reason}
    root = Path(lease["path"])
    identity = row_value(lease, "root_identity")
    if root.is_symlink() or not root.is_dir() or (identity and directory_identity(root) != identity):
        # Gone, or replaced by a directory Tri did not create: delete nothing.
        _lease_update(store, "materialization_leases", lease["id"], status="gone", reaped_at=utcnow())
        result["status"] = "gone"
        return result
    ws = store.get_workspace(lease["workspace_id"])
    before = lease["last_sync_snapshot_id"]
    before_entries = store.load_snapshot_tree(before)
    baseline_file = row_value(lease, "baseline_path")
    if baseline_file and Path(baseline_file).exists():
        materialization = load_materialization(root, Path(baseline_file))
    else:
        materialization = Materialization(path=root, backend=row_value(lease, "backend") or "copy", degraded=True)
    rules = import_rules(store, ws["project_id"], before_entries)
    lease_ignored = loads(lease["ignored_paths_json"], [])
    changed, deleted = materialization.scan(sparse_entries(before_entries, lease_sparse_paths(lease)), ignore=rules)
    captured_changed = [
        path
        for path in changed
        if not path_matches_hidden(path, lease_ignored)
        and not path.startswith(GENERATED_CONFIG_PREFIX + "/")
        and (path in before_entries or not is_secret_like_path(path))
    ]
    captured_deleted = [path for path in deleted if not path_matches_hidden(path, lease_ignored)]
    analysis = classify_uncaptured(root, materialization, before_entries, set(captured_changed), rules, lease_ignored)
    blocked = analysis["secret_like_paths"] or analysis["user_git_dirs"] or analysis["ignored_paths"]
    if blocked and not force:
        attention = {
            "reason": reason,
            "secret_like_paths": analysis["secret_like_paths"][:50],
            "user_git_dirs": analysis["user_git_dirs"][:50],
            "ignored_paths": analysis["ignored_paths"][:50],
            "message": "Directory holds files Tri did not create and will not capture (secret-like files, ignored files outside dependency/build directories, or a .git directory). Remove or move them, or run `tri leases reap <lease> --force`.",
            "detected_at": utcnow(),
        }
        with store.conn:
            store.conn.execute("UPDATE materialization_leases SET status = 'expired_needs_attention', attention_json = ? WHERE id = ?", (dumps(attention), lease["id"]))
            store.create_event(kind="lease.needs_attention", actor=actor, project_id=ws["project_id"], workspace_id=ws["id"], task_id=ws["task_id"], object_refs={"lease": lease["id"], "path": str(root), "reason": reason})
        result.update(status="expired_needs_attention", attention=attention)
        return result
    after_entries = entries_after_scan(store, root, before_entries, captured_changed, captured_deleted, skip_escaping=True)
    paths = changed_paths(before_entries, after_entries)
    salvage = None
    if paths:
        salvage = create_salvage(store, ws, lease_id=lease["id"], kind="mount", base_snapshot=before, entries=after_entries, paths=paths, deleted_ignored=analysis["deleted_ignored"], actor=actor)
    remove_tree(root)
    if baseline_file:
        Path(baseline_file).unlink(missing_ok=True)
    with store.conn:
        store.conn.execute(
            "UPDATE materialization_leases SET status = 'reaped', reaped_at = ?, salvage_id = ?, attention_json = NULL WHERE id = ?",
            (utcnow(), salvage["id"] if salvage else None, lease["id"]),
        )
        store.create_event(
            kind="lease.reaped",
            actor=actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"lease": lease["id"], "path": str(root), "reason": reason, "salvage": salvage["id"] if salvage else None, "forced": force, "deleted_ignored": analysis["deleted_ignored"]},
        )
    result.update(status="reaped", salvage=salvage, deleted_ignored=analysis["deleted_ignored"])
    return result


def run_lease_holder_alive(lease: sqlite3.Row) -> bool:
    lock_path = lease["holder_lock"]
    if not lock_path or not Path(lock_path).exists():
        return False
    probe = FileLock(Path(lock_path))
    try:
        return not probe.acquire(exclusive=True, blocking=False)
    finally:
        probe.close()


def reap_disk_lease(store: Store, lease: sqlite3.Row, *, actor: str) -> dict[str, Any] | None:
    """Reap a run/env-build directory whose owning process is gone. Returns None if still live."""
    if run_lease_holder_alive(lease):
        return None
    with store.conn:
        claimed = store.conn.execute("UPDATE disk_leases SET status = 'reaping' WHERE id = ? AND status = 'active'", (lease["id"],)).rowcount
    if claimed != 1:
        return None
    try:
        return _reap_claimed_disk_lease(store, lease, actor=actor)
    except BaseException:
        with store.conn:
            store.conn.execute("UPDATE disk_leases SET status = 'active' WHERE id = ? AND status = 'reaping'", (lease["id"],))
        raise


def _reap_claimed_disk_lease(store: Store, lease: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    work = Path(lease["path"])
    lease_dir = work.parent
    result: dict[str, Any] = {"lease": lease["id"], "kind": lease["kind"], "path": str(work), "workspace": lease["workspace_id"], "reason": "orphaned"}
    salvage = None
    baseline_file = lease["baseline_path"]
    if (
        lease["kind"] == "run"
        and lease_dir.is_dir()
        and baseline_file
        and Path(baseline_file).exists()
        and lease["before_snapshot_id"]
        and lease["workspace_id"]
    ):
        ws = store.row("SELECT * FROM workspaces WHERE id = ?", (lease["workspace_id"],))
        if ws is not None and ws["status"] in ("active", "paused"):
            materialization = load_any_materialization(work, Path(baseline_file))
            before_entries = store.load_snapshot_tree(lease["before_snapshot_id"])
            rules = import_rules(store, ws["project_id"], before_entries)
            changed, deleted = materialization.scan(sparse_entries(before_entries, lease_sparse_paths(lease)), ignore=rules)
            changed = [path for path in changed if not path.startswith(GENERATED_CONFIG_PREFIX + "/") and (path in before_entries or not is_secret_like_path(path))]
            after_entries = entries_after_scan(store, materialization.content_root, before_entries, changed, deleted, skip_escaping=True)
            paths = changed_paths(before_entries, after_entries)
            if paths:
                salvage = create_salvage(
                    store,
                    ws,
                    lease_id=lease["id"],
                    kind="run",
                    base_snapshot=lease["before_snapshot_id"],
                    entries=after_entries,
                    paths=paths,
                    deleted_ignored={"count": 0, "paths": []},
                    actor=actor,
                )
    remove_tree(lease_dir)
    with store.conn:
        store.conn.execute(
            "UPDATE disk_leases SET status = 'reaped', released_at = ?, salvage_id = ? WHERE id = ?",
            (utcnow(), salvage["id"] if salvage else None, lease["id"]),
        )
        store.create_event(
            kind="lease.reaped",
            actor=actor,
            workspace_id=lease["workspace_id"],
            object_refs={"lease": lease["id"], "kind": lease["kind"], "path": str(work), "reason": "orphaned", "salvage": salvage["id"] if salvage else None},
        )
    result.update(status="reaped", salvage=salvage)
    return result


def _mount_reap_reason(lease: sqlite3.Row, ws_status: str | None, now: str) -> str | None:
    if row_value(lease, "grandfathered", 0):
        return None
    if ws_status == "discarded":
        return "workspace_discarded"
    if lease["status"] == "revoked":
        return "revoked"
    if lease["status"] in ("expired", "expired_needs_attention"):
        return "expired"
    if lease["expires_at"] and lease["expires_at"] <= now:
        return "expired"
    return None


def reap_leases(
    store: Store,
    *,
    actor: str,
    limit: int | None = None,
    force_lease: str | None = None,
    workspace_id: str | None = None,
    reason: str | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """Reap expired/orphaned directories, salvaging unsynced edits first.

    Reapers are serialized by ``reaper.lock``; background passes skip (``busy``)
    instead of waiting, explicit ones wait."""
    reaper_lock = FileLock(store.home / "reaper.lock")
    explicit = force_lease is not None or workspace_id is not None
    if not reaper_lock.acquire(exclusive=True, blocking=explicit):
        reaper_lock.close()
        return {"reaped": [], "salvaged": [], "needs_attention": [], "errors": [], "checked": 0, "busy": True, "swept": []}
    try:
        return _reap_leases_locked(store, actor=actor, limit=limit, force_lease=force_lease, workspace_id=workspace_id, reason=reason, force=force)
    finally:
        reaper_lock.close()


def _reap_leases_locked(
    store: Store,
    *,
    actor: str,
    limit: int | None,
    force_lease: str | None,
    workspace_id: str | None,
    reason: str | None,
    force: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {"reaped": [], "salvaged": [], "needs_attention": [], "errors": [], "checked": 0, "busy": False, "swept": []}

    def record(result: dict[str, Any] | None) -> None:
        if result is None:
            return
        if result.get("status") == "expired_needs_attention":
            report["needs_attention"].append(result)
            return
        report["reaped"].append(result)
        if result.get("salvage"):
            report["salvaged"].append(result["salvage"])

    def attempt(fn: Callable[[], dict[str, Any] | None], lease_id: str) -> None:
        report["checked"] += 1
        try:
            record(fn())
        except TriError as exc:
            report["errors"].append({"lease": lease_id, **exc.diagnostic})
        except OSError as exc:
            report["errors"].append({"lease": lease_id, "code": "reap_failed", "message": str(exc)})

    if force_lease is not None:
        mount = store.row("SELECT * FROM materialization_leases WHERE id = ?", (force_lease,))
        if mount is not None:
            if mount["status"] in ("reaped", "gone"):
                raise TriError("lease_already_reaped", "Lease directory was already reaped.", object=force_lease, status=mount["status"])
            attempt(lambda: reap_mount_lease(store, mount, actor=actor, force=force, reason=reason or "forced"), mount["id"])
            return report
        disk = store.row("SELECT * FROM disk_leases WHERE id = ?", (force_lease,))
        if disk is None:
            raise TriError("path_not_found", "Lease not found.", object=force_lease)
        if disk["status"] != "active":
            raise TriError("lease_already_reaped", "Lease directory was already released.", object=force_lease, status=disk["status"])
        attempt(lambda: reap_disk_lease(store, disk, actor=actor), disk["id"])
        return report

    budget = limit if limit is not None else 1 << 30
    if workspace_id is None:
        for disk in store.rows("SELECT * FROM disk_leases WHERE status = 'active' ORDER BY created_at"):
            if budget <= 0:
                break
            budget -= 1
            attempt(lambda disk=disk: reap_disk_lease(store, disk, actor=actor), disk["id"])
    now = utcnow()
    placeholders = ",".join("?" for _ in REAPABLE_MOUNT_STATUSES)
    params: tuple[Any, ...] = REAPABLE_MOUNT_STATUSES
    sql = f"SELECT * FROM materialization_leases WHERE status IN ({placeholders}) AND reaped_at IS NULL"
    if workspace_id is not None:
        sql += " AND workspace_id = ?"
        params = (*params, workspace_id)
    for mount in store.rows(sql + " ORDER BY created_at, rowid", params):
        if budget <= 0:
            break
        ws = store.row("SELECT status FROM workspaces WHERE id = ?", (mount["workspace_id"],))
        why = reason if workspace_id is not None else _mount_reap_reason(mount, ws["status"] if ws else None, now)
        if why is None:
            continue
        budget -= 1
        attempt(lambda mount=mount, why=why: reap_mount_lease(store, mount, actor=actor, reason=why), mount["id"])
    if workspace_id is None and force_lease is None:
        report["swept"] = sweep_orphan_dirs(store)
    store_meta_set(store, "last_reap_at", utcnow())
    return report


ORPHAN_DIR_MIN_AGE_SECONDS = 600
STAGING_DIR_MIN_AGE_SECONDS = 3600


def sweep_orphan_dirs(store: Store) -> list[str]:
    """Remove run directories no active lease owns (crashes before the lease row
    was written, leaks from older versions) and stale env staging directories."""
    swept: list[str] = []
    now = time.time()
    owned = {Path(row["path"]).parent.name for row in store.rows("SELECT path FROM disk_leases WHERE status IN ('active', 'reaping')")}
    if store.runs.exists():
        for child in store.runs.iterdir():
            if child.name in owned:
                continue
            try:
                if now - child.lstat().st_mtime < ORPHAN_DIR_MIN_AGE_SECONDS:
                    continue
            except OSError:
                continue
            holder = child / ".holder.lock"
            if holder.exists():
                probe = FileLock(holder)
                try:
                    if not probe.acquire(exclusive=True, blocking=False):
                        continue
                finally:
                    probe.close()
            remove_tree(child)
            swept.append(str(child))
    envs = store.home / "envs"
    if envs.exists():
        for child in envs.iterdir():
            if child.name.startswith(".") and child.name.endswith(".tmp"):
                try:
                    if now - child.lstat().st_mtime < STAGING_DIR_MIN_AGE_SECONDS:
                        continue
                except OSError:
                    continue
                remove_tree(child)
                swept.append(str(child))
    return swept


def maybe_auto_reap(store: Store) -> None:
    if os.environ.get("TRI_AUTO_REAP", "1") == "0":
        return
    last = store_meta_get(store, "last_reap_at")
    if last:
        try:
            if (datetime.now(timezone.utc) - parse_utc(last)).total_seconds() < AUTO_REAP_INTERVAL_SECONDS:
                return
        except ValueError:
            pass
    store_meta_set(store, "last_reap_at", utcnow())
    try:
        reap_leases(store, actor=REAPER_ACTOR, limit=AUTO_REAP_LIMIT)
        evict_caches(store)
        empty_trash(store)
    except Exception as exc:  # maintenance never fails the user's command; surface it in doctor
        store_meta_set(store, "last_reap_error", f"{utcnow()} {type(exc).__name__}: {exc}")
        return
    with store.conn:
        store.conn.execute("DELETE FROM store_meta WHERE key = 'last_reap_error'")


def start_reaper_thread(interval: float = SERVE_REAP_INTERVAL_SECONDS) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                reaper_store = Store()
                try:
                    reap_leases(reaper_store, actor=REAPER_ACTOR, limit=50)
                    evict_caches(reaper_store)
                finally:
                    reaper_store.close()
            except Exception:  # never let maintenance kill the API server
                continue

    threading.Thread(target=loop, name="tri-reaper", daemon=True).start()
    return stop


def lease_status_summary(store: Store) -> dict[str, Any]:
    counts = {row["status"]: row["count"] for row in store.rows("SELECT status, COUNT(*) AS count FROM materialization_leases GROUP BY status")}
    runs = {row["status"]: row["count"] for row in store.rows("SELECT status, COUNT(*) AS count FROM disk_leases GROUP BY status")}
    attention = [row["id"] for row in store.rows("SELECT id FROM materialization_leases WHERE status = 'expired_needs_attention' ORDER BY created_at")]
    grandfathered = [
        row["id"]
        for row in store.rows("SELECT id FROM materialization_leases WHERE grandfathered = 1 AND status NOT IN ('reaped', 'gone') ORDER BY created_at")
    ]
    return {
        "mounts_by_status": counts,
        "run_dirs_by_status": runs,
        "needs_attention": attention,
        "grandfathered": grandfathered,
        "last_reap_at": store_meta_get(store, "last_reap_at"),
        "last_reap_error": store_meta_get(store, "last_reap_error"),
    }


def claim_mount_path(store: Store, root: Path, *, actor: str) -> None:
    """One live lease per directory: refuse a path a live lease owns; reap
    (salvaging first) a dead lease that still owns it."""
    now = utcnow()
    for lease in store.rows("SELECT * FROM materialization_leases WHERE path = ? AND reaped_at IS NULL AND status NOT IN ('reaped', 'gone')", (str(root),)):
        live = lease["status"] in ("active", "read_only") and not (lease["expires_at"] and lease["expires_at"] <= now)
        if live or lease["status"] == "expired_needs_attention":
            raise TriError(
                "mount_path_in_use",
                "Another materialization lease owns this directory; sync and release it (tri leases reap <lease> --force) or mount somewhere else.",
                object=str(root),
                lease=lease["id"],
                status=lease["status"],
            )
        report = reap_leases(store, actor=actor, force_lease=lease["id"], reason="remount", force=False)
        if report["needs_attention"] or report["errors"]:
            raise TriError("mount_path_in_use", "The previous lease on this directory could not be reaped safely.", object=str(root), lease=lease["id"], report=report)


def renew_mount_lease(store: Store, lease_id: str, ttl_seconds: int | None = None) -> None:
    lease = store.get_materialization_lease(lease_id)
    ttl = ttl_seconds or row_value(lease, "ttl_seconds")
    if not ttl:
        return
    with store.conn:
        store.conn.execute("UPDATE materialization_leases SET expires_at = ?, ttl_seconds = ? WHERE id = ?", (lease_expiry(ttl), ttl, lease_id))


def cmd_leases_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.workspace:
        ws = store.get_workspace(args.workspace)
        require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
    readable: dict[str, bool] = {}

    def can_read(workspace_id: str | None) -> bool:
        if workspace_id is None:
            return True
        if workspace_id not in readable:
            decision, _ = object_access_decision(store, workspace_id, actor=args.actor, action="workspace.read")
            readable[workspace_id] = bool(decision["allowed"])
        return readable[workspace_id]

    leases: list[dict[str, Any]] = []
    mount_sql = "SELECT * FROM materialization_leases" + (" WHERE workspace_id = ?" if args.workspace else "") + " ORDER BY created_at, rowid"
    for row in store.rows(mount_sql, (args.workspace,) if args.workspace else ()):
        if can_read(row["workspace_id"]):
            leases.append({"kind": "mount", **materialization_lease_data(row)})
    disk_sql = "SELECT * FROM disk_leases" + (" WHERE workspace_id = ?" if args.workspace else "") + " ORDER BY created_at, rowid"
    for row in store.rows(disk_sql, (args.workspace,) if args.workspace else ()):
        if can_read(row["workspace_id"]):
            leases.append(disk_lease_data(row))
    if args.status:
        leases = [lease for lease in leases if lease["status"] == args.status]
    return envelope("tri.lease.list", command, actor=args.actor, start=start, data={"workspace": args.workspace, "leases": leases})


def cmd_leases_renew(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    lease = store.get_materialization_lease(args.lease)
    if lease["actor"] != args.actor:
        require_object_access(store, lease["workspace_id"], actor=args.actor, action="workspace.write")
    if args.ttl_seconds is not None and args.ttl_seconds <= 0:
        raise TriError("invalid_command", "Materialization lease TTL must be positive.", object=lease["id"])
    if lease["status"] in ("reaped", "gone", "revoked"):
        raise TriError("stale_materialization", "Lease can no longer be renewed.", object=lease["id"], status=lease["status"])
    if not Path(lease["path"]).is_dir():
        raise TriError("stale_materialization", "Materialized path no longer exists.", object=lease["path"], lease=lease["id"])
    ttl = args.ttl_seconds or row_value(lease, "ttl_seconds") or DEFAULT_MOUNT_TTL_SECONDS
    ws = store.get_workspace(lease["workspace_id"])
    status = lease["status"]
    if status in ("expired", "expired_needs_attention"):
        status = "read_only" if ws["status"] == "archived" else "active"
    with store.conn:
        store.conn.execute(
            "UPDATE materialization_leases SET expires_at = ?, ttl_seconds = ?, status = ?, attention_json = NULL WHERE id = ?",
            (lease_expiry(ttl), ttl, status, lease["id"]),
        )
        evt = store.create_event(kind="lease.renewed", actor=args.actor, project_id=ws["project_id"], workspace_id=ws["id"], task_id=ws["task_id"], object_refs={"lease": lease["id"], "ttl_seconds": ttl})
    return envelope("tri.workspace.materialization_lease", command, actor=args.actor, start=start, events=[evt], data=materialization_lease_data(store.get_materialization_lease(lease["id"])))


def cmd_leases_reap(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.lease:
        mount = store.row("SELECT * FROM materialization_leases WHERE id = ?", (args.lease,))
        workspace_id = mount["workspace_id"] if mount is not None else None
        if mount is None:
            disk = store.row("SELECT * FROM disk_leases WHERE id = ?", (args.lease,))
            workspace_id = disk["workspace_id"] if disk is not None else None
        if workspace_id and (mount is None or mount["actor"] != args.actor):
            require_object_access(store, workspace_id, actor=args.actor, action="workspace.write")
        if not args.force:
            raise TriError("invalid_command", "Reaping one lease deletes its directory; pass --force to confirm.", object=args.lease)
        report = reap_leases(store, actor=args.actor, force_lease=args.lease)
    else:
        report = reap_leases(store, actor=args.actor)
    return envelope("tri.lease.reap", command, actor=args.actor, start=start, data=report)


def cmd_salvage_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
    rows = store.rows("SELECT * FROM salvages WHERE workspace_id = ? ORDER BY created_at, rowid", (ws["id"],))
    return envelope("tri.salvage.list", command, actor=args.actor, start=start, data={"workspace": ws["id"], "salvages": [salvage_data(row) for row in rows]})


def cmd_salvage_adopt(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = store.row("SELECT * FROM salvages WHERE id = ?", (args.salvage,))
    if row is None:
        raise TriError("path_not_found", "Salvage not found.", object=args.salvage)
    ws = store.get_workspace(row["workspace_id"])
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can adopt salvaged edits.", object=ws["id"], status=ws["status"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    if row["adopted_at"]:
        raise TriError("salvage_already_adopted", "Salvage was already adopted.", object=row["id"], adopted_at=row["adopted_at"])
    head = ws["head_snapshot_id"]
    if args.expect_head and args.expect_head != head:
        raise TriError("stale_workspace_head", "Workspace head does not match --expect-head.", object=ws["id"], expected=args.expect_head, actual=head, retryable=True)
    if head != row["base_snapshot_id"]:
        raise TriError(
            "stale_workspace_head",
            "Workspace head moved after these edits were salvaged; adopt applies only onto the snapshot they were salvaged from. Compare with `tri diff <base> <salvage-snapshot>` and re-apply the paths you need.",
            object=ws["id"],
            expected=row["base_snapshot_id"],
            actual=head,
            salvage_snapshot=row["snapshot_id"],
            retryable=False,
        )
    paths = loads(row["paths_json"], [])
    with store.conn:
        op_id = new_id("op")
        store.conn.execute(
            "INSERT INTO operations (id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id, path_set_json, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, ws["id"], args.actor, "salvage_adopt", head, row["snapshot_id"], dumps(paths), dumps({"salvage": row["id"], "lease": row["lease_id"]}), utcnow()),
        )
        updated = store.conn.execute(
            "UPDATE workspaces SET head_snapshot_id = ?, updated_at = ? WHERE id = ? AND head_snapshot_id = ?",
            (row["snapshot_id"], utcnow(), ws["id"], head),
        )
        if updated.rowcount != 1:
            raise TriError("stale_workspace_head", "Workspace head changed while adopting salvage.", object=ws["id"], expected=head, retryable=True)
        store.conn.execute("UPDATE salvages SET adopted_at = ?, adopted_by = ? WHERE id = ?", (utcnow(), args.actor, row["id"]))
        evt = store.create_event(
            kind="salvage.adopted",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"salvage": row["id"], "operation": op_id, "snapshot": row["snapshot_id"], "paths": paths},
            input_fingerprints={"snapshot": head},
            output_fingerprints={"snapshot": row["snapshot_id"]},
        )
    return envelope(
        "tri.salvage.adopt",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={"salvage": row["id"], "workspace": ws["id"], "operation": op_id, "snapshot": row["snapshot_id"], "paths": paths},
    )


ENV_MODES = ("auto", "copy", "link", "private")
_ENV_KEY_COMMAND_CACHE: dict[str, str] = {}


def split_command(command_text: str) -> list[str]:
    """Recipe commands use POSIX shell-style quoting on every platform and run
    without a shell (use ``sh -c``/``cmd /c`` for shell features)."""
    return shlex.split(command_text, posix=True)


def env_platform_tag() -> str:
    import platform as _platform

    libc = "-".join(part for part in _platform.libc_ver() if part) or "nolibc"
    return f"{sys.platform}-{_platform.machine()}-{libc}"


def env_recipe_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": row["project_id"],
        "name": row["name"],
        "key_files": loads(row["key_files_json"], []),
        "key_commands": loads(row["key_commands_json"], []),
        "install": row["install"],
        "outputs": loads(row["outputs_json"], []),
        "mode": row["mode"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def env_build_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "recipe": row["recipe_id"],
        "status": row["status"],
        "path": row["path"],
        "outputs": loads(row["outputs_json"], []),
        "size_bytes": row["size_bytes"],
        "exit_status": row["exit_status"],
        "stdout_artifact": row["stdout_artifact"],
        "stderr_artifact": row["stderr_artifact"],
        "error": loads(row["error_json"], None) if row["error_json"] else None,
        "workspace": row["workspace_id"],
        "built_by": row["built_by"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def env_key_command_output(command_text: str) -> str:
    cached = _ENV_KEY_COMMAND_CACHE.get(command_text)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(split_command(command_text), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60, check=False)
        output = f"exit={proc.returncode}\n" + proc.stdout.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = f"error={type(exc).__name__}"
    _ENV_KEY_COMMAND_CACHE[command_text] = output
    return output


def env_key(recipe: sqlite3.Row, entries: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Content key for an environment: recipe + key-file contents + key-command outputs + platform."""
    files: dict[str, str | None] = {}
    for pattern in loads(recipe["key_files_json"], []):
        matched = [path for path in entries if path == pattern or fnmatch.fnmatch(path, pattern)]
        if not matched:
            files[pattern] = None
        for path in matched:
            entry = entries[path]
            files[path] = entry.get("hash") or entry.get("blob")
    inputs = {
        "recipe": {
            "install": recipe["install"],
            "outputs": loads(recipe["outputs_json"], []),
            "key_files": loads(recipe["key_files_json"], []),
            "key_commands": loads(recipe["key_commands_json"], []),
        },
        "files": files,
        "commands": {command_text: env_key_command_output(command_text) for command_text in loads(recipe["key_commands_json"], [])},
        "platform": env_platform_tag(),
    }
    return f"env_{sha256(dumps(inputs).encode())[:24]}", inputs


def freeze_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            try:
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
            except OSError:
                pass
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o555)
                except OSError:
                    pass


def tree_size_bytes(root: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _record_env_build(store: Store, key: str, recipe: sqlite3.Row, **fields: Any) -> None:
    now = utcnow()
    existing = store.row("SELECT key FROM env_builds WHERE key = ?", (key,))
    with store.conn:
        if existing is None:
            store.conn.execute(
                "INSERT INTO env_builds (key, recipe_id, project_id, status, path, outputs_json, created_at, last_used_at) VALUES (?, ?, ?, 'building', '', ?, ?, ?)",
                (key, recipe["id"], recipe["project_id"], recipe["outputs_json"], now, now),
            )
        if fields:
            columns = ", ".join(f"{name} = ?" for name in fields)
            store.conn.execute(f"UPDATE env_builds SET {columns} WHERE key = ?", (*fields.values(), key))


def claim_env_for_use(store: Store, key: str) -> bool:
    """Mark a ready env as just used; fails if eviction claimed it first."""
    with store.conn:
        return store.conn.execute("UPDATE env_builds SET last_used_at = ? WHERE key = ? AND status = 'ready'", (utcnow(), key)).rowcount == 1


def claim_env_for_eviction(store: Store, row: sqlite3.Row) -> bool:
    """Claim an env for eviction only if nobody used it since ``row`` was read."""
    with store.conn:
        return (
            store.conn.execute(
                "UPDATE env_builds SET status = 'evicting' WHERE key = ? AND status = 'ready' AND last_used_at = ?",
                (row["key"], row["last_used_at"]),
            ).rowcount
            == 1
        )


def ensure_env(store: Store, recipe: sqlite3.Row, entries: dict[str, dict[str, Any]], *, actor: str, workspace: sqlite3.Row | None) -> tuple[sqlite3.Row, str]:
    """Return a ready shared environment for ``entries``, building it once per key."""
    key, inputs = env_key(recipe, entries)
    envs_root = store.home / "envs"
    envs_root.mkdir(parents=True, exist_ok=True)

    def ready_row() -> sqlite3.Row | None:
        row = store.row("SELECT * FROM env_builds WHERE key = ?", (key,))
        if row is not None and row["status"] == "ready" and Path(row["path"]).is_dir():
            return row
        return None

    row = ready_row()
    if row is not None and claim_env_for_use(store, key):
        return store.row("SELECT * FROM env_builds WHERE key = ?", (key,)), "reused"
    key_lock = FileLock(envs_root / f".{key}.lock")
    key_lock.acquire(exclusive=True)
    try:
        row = ready_row()
        if row is not None and claim_env_for_use(store, key):
            return store.row("SELECT * FROM env_builds WHERE key = ?", (key,)), "reused"
        _record_env_build(store, key, recipe, status="building", error_json=None)
        build = RunLease(store, kind="env_build", key=f"envbuild_{key}_{uuid.uuid4().hex[:8]}", workspace_id=workspace["id"] if workspace is not None else None, before_snapshot=None)
        try:
            materialization = materialize_entries(store, entries, build.work)
            build.record_materialization(materialization)
            argv = split_command(recipe["install"])
            # Installs run hooks from workspace content (npm/pip scripts), so they share the run sandbox; they usually need the network.
            box = command_sandbox(store, build.dir, purpose="env_build", network=os.environ.get("TRI_RUN_SANDBOX_ENV_NETWORK", "1") == "1")
            try:
                proc = sandbox.run(box, argv, cwd=build.work, env=sandbox_env(box, build.dir, os.environ, {}))
                exit_status, out, err = proc.returncode, proc.stdout, proc.stderr
            except OSError as exc:
                exit_status, out, err = 127, b"", str(exc).encode()
            with store.conn:
                stdout_artifact = write_artifact(store, "stdout", f"{key}-{build.id}-stdout.txt", out, labels=["env-build"])
                stderr_artifact = write_artifact(store, "stderr", f"{key}-{build.id}-stderr.txt", err, labels=["env-build"])
            common = {"recipe": recipe["name"], "key": key, "stdout_artifact": stdout_artifact, "stderr_artifact": stderr_artifact}
            if exit_status != 0:
                error = {"code": "env_build_failed", "exit_status": exit_status}
                _record_env_build(store, key, recipe, status="failed", exit_status=exit_status, stdout_artifact=stdout_artifact, stderr_artifact=stderr_artifact, error_json=dumps(error), built_by=actor)
                raise TriError("env_build_failed", f"Environment '{recipe['name']}' install command failed.", object=recipe["name"], exit_status=exit_status, retryable=True, **common)
            outputs = loads(recipe["outputs_json"], [])
            missing = [out_path for out_path in outputs if not os.path.lexists(build.work / out_path)]
            if missing:
                error = {"code": "env_output_missing", "outputs": missing}
                _record_env_build(store, key, recipe, status="failed", exit_status=exit_status, stdout_artifact=stdout_artifact, stderr_artifact=stderr_artifact, error_json=dumps(error), built_by=actor)
                raise TriError("env_output_missing", f"Environment '{recipe['name']}' install did not produce its declared outputs.", object=recipe["name"], outputs=missing, retryable=False, **common)
            staging = envs_root / f".{key}.{uuid.uuid4().hex}.tmp"
            for out_path in outputs:
                destination = staging / out_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(build.work / out_path, destination)
            freeze_read_only(staging)
            final = envs_root / key
            if final.exists():
                remove_tree(final)
            os.rename(staging, final)
            _record_env_build(
                store,
                key,
                recipe,
                status="ready",
                path=str(final),
                size_bytes=tree_size_bytes(final),
                exit_status=exit_status,
                stdout_artifact=stdout_artifact,
                stderr_artifact=stderr_artifact,
                built_by=actor,
                workspace_id=workspace["id"] if workspace is not None else None,
                last_used_at=utcnow(),
            )
            with store.conn:
                store.create_event(
                    kind="env.built",
                    actor=actor,
                    project_id=recipe["project_id"],
                    workspace_id=workspace["id"] if workspace is not None else None,
                    object_refs={"recipe": recipe["id"], "name": recipe["name"], "key": key, "inputs": inputs["files"], "platform": inputs["platform"]},
                )
        finally:
            build.release()
        row = ready_row()
        assert row is not None
        return row, "built"
    finally:
        key_lock.close()


class EnvPlan:
    def __init__(self) -> None:
        self.attachments: list[Attachment] = []
        self.reserved: list[str] = []
        self.private: list[sqlite3.Row] = []
        self.records: list[dict[str, Any]] = []

    def finalize(self, materialization: Materialization) -> None:
        materialization.skip_prefixes = sorted(set(materialization.skip_prefixes) | set(self.reserved))

    def install_private(self, work: Path, env: dict[str, str], box: "sandbox.Sandbox | None" = None) -> None:
        for recipe in self.private:
            try:
                proc = sandbox.run(box, split_command(recipe["install"]), cwd=work, env=env)
                exit_status = proc.returncode
            except OSError:
                exit_status = 127
            if exit_status != 0:
                raise TriError("env_build_failed", f"Environment '{recipe['name']}' per-run install failed.", object=recipe["name"], exit_status=exit_status, mode="private", retryable=True)


def plan_environments(
    store: Store,
    project_id: str,
    entries: dict[str, dict[str, Any]],
    *,
    actor: str,
    workspace: sqlite3.Row | None,
    purpose: str,
    env_link: bool = False,
) -> EnvPlan:
    plan = EnvPlan()
    for recipe in store.rows("SELECT * FROM env_recipes WHERE project_id = ? ORDER BY name", (project_id,)):
        outputs = loads(recipe["outputs_json"], [])
        free = [out_path for out_path in outputs if not any(path == out_path or path.startswith(out_path + "/") for path in entries)]
        conflicting = sorted(set(outputs) - set(free))
        plan.reserved.extend(free)
        if recipe["mode"] == "private":
            if purpose == "run":
                plan.private.append(recipe)
            plan.records.append({"recipe": recipe["name"], "key": None, "status": "per_run" if purpose == "run" else "skipped", "mode": "private", "outputs": free, "conflicting_outputs": conflicting})
            continue
        build, status = ensure_env(store, recipe, entries, actor=actor, workspace=workspace)
        if recipe["mode"] == "auto":
            attach_mode = "auto" if purpose == "run" else ("link" if env_link else "clone-only")
        else:
            attach_mode = recipe["mode"]
        for out_path in free:
            plan.attachments.append(Attachment(out_path, Path(build["path"]) / out_path, attach_mode))
        plan.records.append({"recipe": recipe["name"], "key": build["key"], "status": status, "mode": attach_mode, "outputs": free, "conflicting_outputs": conflicting})
    return plan


def require_env_maintainer(args: argparse.Namespace) -> None:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can change environment recipes.", object=args.project, action="env.define", required_grant="role:maintainer")


def get_env_recipe(store: Store, project_id: str, name: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM env_recipes WHERE project_id = ? AND (name = ? OR id = ?)", (project_id, name, name))
    if row is None:
        raise TriError("path_not_found", "Environment recipe not found.", object=name)
    return row


def cmd_env_define(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_env_maintainer(args)
    project = store.get_project(args.project)
    if args.mode not in ENV_MODES:
        raise TriError("invalid_command", "Unknown environment mode.", object=args.name, allowed=list(ENV_MODES))
    outputs = sorted({normalize_path(out_path) for out_path in args.output or []})
    if not outputs:
        raise TriError("invalid_command", "An environment needs at least one --output path.", object=args.name)
    if not split_command(args.install):
        raise TriError("invalid_command", "An environment needs a non-empty --install command.", object=args.name)
    key_files = sorted({normalize_path(path) for path in args.key_file or []})
    key_commands = list(dict.fromkeys(args.key_command or []))
    existing = store.row("SELECT id FROM env_recipes WHERE project_id = ? AND name = ?", (project["id"], args.name))
    recipe_id = existing["id"] if existing else new_id("envr")
    with store.conn:
        store.conn.execute(
            """
            INSERT INTO env_recipes (id, project_id, name, key_files_json, key_commands_json, install, outputs_json, mode, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, name) DO UPDATE SET key_files_json = excluded.key_files_json, key_commands_json = excluded.key_commands_json,
              install = excluded.install, outputs_json = excluded.outputs_json, mode = excluded.mode
            """,
            (recipe_id, project["id"], args.name, dumps(key_files), dumps(key_commands), args.install, dumps(outputs), args.mode, args.actor, utcnow()),
        )
        evt = store.create_event(kind="env.defined", actor=args.actor, project_id=project["id"], object_refs={"recipe": recipe_id, "name": args.name})
    return envelope("tri.env.recipe", command, actor=args.actor, start=start, events=[evt], data=env_recipe_data(get_env_recipe(store, project["id"], args.name)))


def cmd_env_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    recipes = [env_recipe_data(row) for row in store.rows("SELECT * FROM env_recipes WHERE project_id = ? ORDER BY name", (project["id"],))]
    return envelope("tri.env.list", command, actor=args.actor, start=start, data={"project": project["id"], "recipes": recipes})


def cmd_env_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    recipe = get_env_recipe(store, project["id"], args.name)
    builds = [env_build_data(row) for row in store.rows("SELECT * FROM env_builds WHERE recipe_id = ? ORDER BY created_at, rowid", (recipe["id"],))]
    return envelope("tri.env.inspect", command, actor=args.actor, start=start, data={"recipe": env_recipe_data(recipe), "builds": builds})


def cmd_env_remove(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_env_maintainer(args)
    project = store.get_project(args.project)
    recipe = get_env_recipe(store, project["id"], args.name)
    with store.conn:
        store.conn.execute("DELETE FROM env_recipes WHERE id = ?", (recipe["id"],))
        evt = store.create_event(kind="env.removed", actor=args.actor, project_id=project["id"], object_refs={"recipe": recipe["id"], "name": recipe["name"]})
    recipes = [env_recipe_data(row) for row in store.rows("SELECT * FROM env_recipes WHERE project_id = ? ORDER BY name", (project["id"],))]
    return envelope("tri.env.list", command, actor=args.actor, start=start, events=[evt], data={"project": project["id"], "recipes": recipes})


GC_RETENTION_DAYS = 14
GC_GRACE_SECONDS = 3600
GC_MIN_GRACE_SECONDS = 60
ROOT_SNAPSHOT_SOURCES = ("imported", "merge")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def iso_before(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def gc_live_snapshots(store: Store, *, retention_cutoff: str, grace_cutoff: str) -> set[str]:
    """Snapshots GC must keep: target history, everything a live object references,
    and anything newer than the grace cutoff."""
    live: set[str] = set()

    def add(*values: Any) -> None:
        for value in values:
            if isinstance(value, str) and value:
                live.add(value)

    for row in store.rows("SELECT snapshot_id FROM targets"):
        add(row["snapshot_id"])
    placeholders = ",".join("?" for _ in ROOT_SNAPSHOT_SOURCES)
    for row in store.rows(f"SELECT id FROM snapshots WHERE source IN ({placeholders}) OR created_at > ?", (*ROOT_SNAPSHOT_SOURCES, grace_cutoff)):
        add(row["id"])
    retired_ws = {
        row["id"]
        for row in store.rows("SELECT id FROM workspaces WHERE status = 'discarded' AND updated_at <= ?", (retention_cutoff,))
    }
    live_ws: set[str] = set()
    for row in store.rows("SELECT id, base_snapshot_id, head_snapshot_id, materialized_snapshot_id FROM workspaces"):
        if row["id"] in retired_ws:
            continue
        live_ws.add(row["id"])
        add(row["base_snapshot_id"], row["head_snapshot_id"], row["materialized_snapshot_id"])
    for row in store.rows("SELECT workspace_id, input_snapshot_id, output_snapshot_id FROM operations"):
        if row["workspace_id"] in live_ws:
            add(row["input_snapshot_id"], row["output_snapshot_id"])
    live_cs: set[str] = set()
    for row in store.rows("SELECT id, status, base_snapshot_id, head_snapshot_id, lifecycle_json, created_at FROM changesets"):
        retired_at = loads(row["lifecycle_json"], {}).get("updated_at") or row["created_at"]
        if row["status"] in ("discarded", "superseded") and retired_at <= retention_cutoff:
            continue
        live_cs.add(row["id"])
        add(row["base_snapshot_id"], row["head_snapshot_id"])
    for row in store.rows("SELECT workspace_id, changeset_id, snapshot_before, snapshot_after FROM check_runs"):
        if row["workspace_id"] in live_ws or row["changeset_id"] in live_cs:
            add(row["snapshot_before"], row["snapshot_after"])
    for row in store.rows("SELECT workspace_id, base_snapshot_id, head_snapshot_id, target_snapshot_id FROM workspace_conflicts"):
        if row["workspace_id"] in live_ws:
            add(row["base_snapshot_id"], row["head_snapshot_id"], row["target_snapshot_id"])
    for row in store.rows("SELECT workspace_id, base_snapshot_id, snapshot_id FROM salvages"):
        if row["workspace_id"] in live_ws:
            add(row["base_snapshot_id"], row["snapshot_id"])
    for row in store.rows("SELECT last_sync_snapshot_id, mounted_snapshot_id FROM materialization_leases WHERE status NOT IN ('reaped', 'gone')"):
        add(row["last_sync_snapshot_id"], row["mounted_snapshot_id"])
    return live


def gc_compute(store: Store, *, retention_days: float, grace_seconds: float) -> dict[str, Any]:
    retention_cutoff = iso_before(retention_days * 86400)
    grace_cutoff = iso_before(grace_seconds)
    live_snapshots = gc_live_snapshots(store, retention_cutoff=retention_cutoff, grace_cutoff=grace_cutoff)
    roots: list[str] = []
    for snapshot in live_snapshots:
        row = store.row("SELECT root_tree_id FROM snapshots WHERE id = ?", (snapshot,))
        if row is not None:
            roots.append(row["root_tree_id"])
    live_trees, live_blobs = store.collect_references(roots)
    blob_rows = [
        row
        for row in store.rows("SELECT id, hash, size, object_path, created_at, last_referenced_at FROM blobs WHERE purged_at IS NULL")
        if row["id"] not in live_blobs and (row["last_referenced_at"] or row["created_at"]) <= grace_cutoff
    ]
    tree_rows = [
        row
        for row in store.rows("SELECT id, created_at, last_referenced_at FROM trees WHERE purged_at IS NULL")
        if row["id"] not in live_trees and (row["last_referenced_at"] or row["created_at"]) <= grace_cutoff
    ]
    return {
        "retention_cutoff": retention_cutoff,
        "grace_cutoff": grace_cutoff,
        "live_snapshots": len(live_snapshots),
        "blob_rows": blob_rows,
        "tree_rows": tree_rows,
    }


def blob_object_path(store: Store, row: sqlite3.Row) -> Path:
    stored = row["object_path"]
    if stored:
        path = Path(stored)
        return path if path.is_absolute() else store.blobs / stored
    return store.blobs / row["hash"][:2] / row["hash"]


def gc_plan_summary(store: Store, computed: dict[str, Any]) -> dict[str, Any]:
    disk_bytes = 0
    for row in computed["blob_rows"]:
        try:
            disk_bytes += blob_object_path(store, row).stat().st_size
        except OSError:
            pass
    return {
        "retention_cutoff": computed["retention_cutoff"],
        "grace_cutoff": computed["grace_cutoff"],
        "live_snapshots": computed["live_snapshots"],
        "blobs": {"count": len(computed["blob_rows"]), "content_bytes": sum(row["size"] for row in computed["blob_rows"]), "disk_bytes": disk_bytes},
        "trees": {"count": len(computed["tree_rows"])},
    }


def gc_apply(store: Store, *, retention_days: float, grace_seconds: float, _after_mark: Callable[[], None] | None = None) -> dict[str, Any]:
    """Purge unreachable content. Runs in one write transaction so writers that
    dedup against a candidate either finish first (and protect it) or wait and
    then restore it."""
    gc_lock = FileLock(store.home / "gc.lock")
    if not gc_lock.acquire(exclusive=True, blocking=False):
        gc_lock.close()
        raise TriError("gc_in_progress", "Another garbage collection is running.", retryable=True)
    try:
        store.conn.commit()
        # Mark without holding the write lock. Start on a fresh second so any
        # object touched after the mark began has last_referenced_at >= mark_start.
        start_second = utcnow()
        while utcnow() == start_second:
            time.sleep(0.05)
        mark_start = utcnow()
        computed = gc_compute(store, retention_days=retention_days, grace_seconds=grace_seconds)
        if _after_mark is not None:
            _after_mark()
        store.conn.commit()
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            now = utcnow()
            freed = 0
            purged_blobs = 0
            purged_trees = 0
            purged_hashes: list[str] = []
            purged_tree_ids: list[str] = []
            for row in computed["blob_rows"]:
                current = store.conn.execute("SELECT purged_at, created_at, last_referenced_at FROM blobs WHERE id = ?", (row["id"],)).fetchone()
                if current is None or current["purged_at"] or (current["last_referenced_at"] or current["created_at"]) >= mark_start:
                    continue
                purged_blobs += 1
                purged_hashes.append(row["hash"])
                store.conn.execute("UPDATE blobs SET purged_at = ? WHERE id = ?", (now, row["id"]))
                path = blob_object_path(store, row)
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except FileNotFoundError:
                    pass
                store._blob_cache.pop(row["id"], None)
            for row in computed["tree_rows"]:
                current = store.conn.execute("SELECT purged_at, created_at, last_referenced_at FROM trees WHERE id = ?", (row["id"],)).fetchone()
                if current is None or current["purged_at"] or (current["last_referenced_at"] or current["created_at"]) >= mark_start:
                    continue
                purged_trees += 1
                purged_tree_ids.append(row["id"])
                store.conn.execute("UPDATE trees SET purged_at = ?, entries_json = '' WHERE id = ?", (now, row["id"]))
            store._tree_cache.clear()
            store.create_event(
                kind="store.gc",
                actor=REAPER_ACTOR,
                object_refs={"blobs": purged_blobs, "trees": purged_trees, "freed_bytes": freed, "retention_cutoff": computed["retention_cutoff"]},
            )
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        # Purged bytes must not linger in derived caches (matters when GC is used
        # to scrub leaked content).
        for digest in purged_hashes:
            for suffix in ("", ".x"):
                try:
                    (store.checkout / digest[:2] / (digest + suffix)).unlink()
                except FileNotFoundError:
                    pass
        for tree_id in purged_tree_ids:
            for name in (tree_id, f"{tree_id}.links"):
                base = store.home / "bases" / name
                if base.exists():
                    remove_tree(base)
        return {"blobs": purged_blobs, "trees": purged_trees, "freed_bytes": freed}
    finally:
        gc_lock.close()


def active_lease_env_keys(store: Store) -> set[str]:
    keys: set[str] = set()
    for row in store.rows("SELECT env_keys_json FROM disk_leases WHERE status = 'active'"):
        keys.update(loads(row["env_keys_json"], []))
    for row in store.rows("SELECT env_keys_json FROM materialization_leases WHERE status NOT IN ('reaped', 'gone')"):
        keys.update(loads(row["env_keys_json"], []) if row["env_keys_json"] else [])
    return keys


def active_overlay_lowers(store: Store) -> set[str]:
    lowers: set[str] = set()
    for row in store.rows("SELECT baseline_path FROM disk_leases WHERE status = 'active' AND backend = 'overlay'"):
        try:
            payload = json.loads(Path(row["baseline_path"]).read_text())
            lowers.update(payload.get("overlay", {}).get("lowers", []))
        except (OSError, ValueError, TypeError):
            continue
    return lowers


def live_workspace_hashes(store: Store) -> set[str]:
    hashes: set[str] = set()
    snapshots = {row["snapshot_id"] for row in store.rows("SELECT snapshot_id FROM targets")}
    for row in store.rows("SELECT base_snapshot_id, head_snapshot_id FROM workspaces WHERE status IN ('active', 'paused')"):
        snapshots.update([row["base_snapshot_id"], row["head_snapshot_id"]])
    for snapshot in snapshots:
        try:
            for entry in store.load_snapshot_tree(snapshot).values():
                if entry.get("hash"):
                    hashes.add(entry["hash"])
        except TriError:
            continue
    return hashes


def evict_caches(store: Store, *, clear: bool = False) -> dict[str, Any]:
    """Keep derived caches (checkout cache, base dirs, env builds) within budgets,
    never removing what a live lease depends on. ``clear`` drops everything else."""
    report: dict[str, Any] = {"envs": [], "bases_removed": [], "checkout_cache_bytes_freed": 0}
    # Environment builds: LRU beyond the byte budget.
    env_budget = -1 if clear else env_int("TRI_ENV_CACHE_MAX_BYTES", 10 * 1024**3)
    grace = utcnow() if clear else iso_before(env_int("TRI_ENV_EVICT_GRACE_SECONDS", 600))
    protected = active_lease_env_keys(store)
    ready = store.rows("SELECT * FROM env_builds WHERE status = 'ready' ORDER BY last_used_at, created_at")
    total = sum(row["size_bytes"] or 0 for row in ready)
    for row in ready:
        if total <= env_budget:
            break
        if row["key"] in protected or row["last_used_at"] > grace:
            continue
        if not claim_env_for_eviction(store, row):
            continue
        remove_tree(Path(row["path"]))
        remove_tree(Path(row["path"] + ".layer"))
        with store.conn:
            store.conn.execute("UPDATE env_builds SET status = 'evicted' WHERE key = ?", (row["key"],))
        total -= row["size_bytes"] or 0
        report["envs"].append({"key": row["key"], "size_bytes": row["size_bytes"]})
    # Base directories: keep the most recently used N.
    bases_root = store.home / "bases"
    if bases_root.exists():
        keep = 0 if clear else env_int("TRI_BASE_CACHE_MAX_COUNT", 8)
        in_use = active_overlay_lowers(store)
        stale_tmp = iso_before(3600)
        candidates = []
        for child in bases_root.iterdir():
            if child.name.startswith("."):
                modified = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                if child.name.endswith(".tmp") and modified < stale_tmp:
                    remove_tree(child)
                continue
            if str(child) in in_use:
                continue
            candidates.append(child)
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for child in candidates[keep:]:
            remove_tree(child)
            report["bases_removed"].append(child.name)
    # Checkout cache: drop blobs no live workspace needs, then everything if still over.
    cache = store.materializer.cache
    budget = -1 if clear else env_int("TRI_CHECKOUT_CACHE_MAX_BYTES", 2 * 1024**3)
    size = cache.size_bytes()
    if size > budget and cache.root.exists():
        live = live_workspace_hashes(store)
        freed = 0
        files = [Path(dirpath) / name for dirpath, _, names in os.walk(cache.root) for name in names]
        for keep_live in (True, False):
            for path in files:
                if not path.exists():
                    continue
                digest = path.name.removesuffix(".x")
                if keep_live and digest in live:
                    continue
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except OSError:
                    pass
            if size - freed <= budget:
                break
        report["checkout_cache_bytes_freed"] = freed
    return report


def cmd_gc(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    retention = GC_RETENTION_DAYS if args.retention_days is None else args.retention_days
    grace = GC_GRACE_SECONDS if args.grace_seconds is None else args.grace_seconds
    if retention < 0 or grace < 0:
        raise TriError("invalid_command", "Retention and grace must be non-negative.")
    if grace < GC_MIN_GRACE_SECONDS and not args.unsafe_no_grace:
        raise TriError(
            "invalid_command",
            f"A grace period under {GC_MIN_GRACE_SECONDS} s can purge content a command has written but not yet committed; pass --unsafe-no-grace only when nothing else is using the store.",
            grace_seconds=grace,
        )
    data: dict[str, Any] = {"applied": bool(args.apply), "reaped": None, "evicted": None, "purged": None}
    if args.reap or args.apply:
        data["reaped"] = reap_leases(store, actor=args.actor)
        data["evicted"] = evict_caches(store)
        data["trash_removed"] = empty_trash(store, min_age_seconds=0 if args.apply else 600.0)
    if args.clear_caches:
        data["evicted"] = evict_caches(store, clear=True)
    data["plan"] = gc_plan_summary(store, gc_compute(store, retention_days=retention, grace_seconds=grace))
    if args.apply:
        data["purged"] = gc_apply(store, retention_days=retention, grace_seconds=grace)
    return envelope("tri.gc", command, actor=args.actor, start=start, data=data)


def cmd_workspace_update(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can update base.", object=ws["id"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    if getattr(args, "expect_head", None) and args.expect_head != ws["head_snapshot_id"]:
        raise_stale_workspace_head(store, ws["id"], args.expect_head, "Workspace head changed before update could start.")
    target_snapshot = resolve_target_snapshot(store, ws["project_id"], args.from_ref)
    if target_snapshot == ws["base_snapshot_id"]:
        return envelope(
            "tri.workspace.update",
            command,
            actor=args.actor,
            start=start,
            data={"workspace": ws["id"], "base_snapshot": ws["base_snapshot_id"], "head_snapshot": ws["head_snapshot_id"], "changed_paths": [], "conflicts": []},
        )
    base_entries = store.load_snapshot_tree(ws["base_snapshot_id"])
    head_entries = store.load_snapshot_tree(ws["head_snapshot_id"])
    target_entries = store.load_snapshot_tree(target_snapshot)
    text_merged: list[str] = []
    merged_entries, conflicts, changed = merge_entries(store, base_entries, head_entries, target_entries, text_merged)
    if conflicts:
        conflict_id, evt = create_workspace_conflict(store, ws, target_ref=args.from_ref, target_snapshot=target_snapshot, conflicts=conflicts, actor=args.actor)
        conflict_view = workspace_conflict_data(store, store.get_workspace_conflict(conflict_id))["conflicts"]
        diagnostic = TriError(
            "conflict_required",
            "Workspace update requires conflict resolution.",
            object=ws["id"],
            conflict=conflict_id,
            conflicts=conflict_view,
        ).diagnostic
        return envelope(
            "tri.workspace.update",
            command,
            actor=args.actor,
            start=start,
            ok=False,
            events=[evt],
            diagnostics=[diagnostic],
            data={"workspace": ws["id"], "conflict": conflict_id, "base_snapshot": ws["base_snapshot_id"], "head_snapshot": ws["head_snapshot_id"], "target_snapshot": target_snapshot, "conflicts": conflict_view},
        )
    with store.conn:
        snapshot = store.create_snapshot(
            ws["project_id"],
            merged_entries,
            parents=[ws["head_snapshot_id"], target_snapshot],
            source="merge",
            actor=args.actor,
            metadata={"workspace": ws["id"], "operation": "update_base", "target": args.from_ref},
        )
        op_id = new_id("op")
        store.conn.execute(
            "INSERT INTO operations (id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id, path_set_json, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, ws["id"], args.actor, "update_base", ws["head_snapshot_id"], snapshot["id"], dumps(changed), dumps({"from": args.from_ref, "new_base": target_snapshot}), utcnow()),
        )
        updated = store.conn.execute(
            "UPDATE workspaces SET base_snapshot_id = ?, head_snapshot_id = ?, updated_at = ? WHERE id = ? AND head_snapshot_id = ?",
            (target_snapshot, snapshot["id"], utcnow(), ws["id"], ws["head_snapshot_id"]),
        )
        if updated.rowcount != 1:
            raise_stale_workspace_head(store, ws["id"], ws["head_snapshot_id"], "Workspace head changed before update could commit.")
        evt = store.create_event(
            kind="workspace.update_base",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"operation": op_id, "snapshot": snapshot["id"], "target_snapshot": target_snapshot, "paths": changed, "text_merged_paths": text_merged},
            input_fingerprints={"base_snapshot": ws["base_snapshot_id"], "head_snapshot": ws["head_snapshot_id"]},
            output_fingerprints={"base_snapshot": target_snapshot, "head_snapshot": snapshot["id"]},
        )
    return envelope(
        "tri.workspace.update",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={"workspace": ws["id"], "operation": op_id, "base_snapshot": target_snapshot, "head_snapshot": snapshot["id"], "changed_paths": changed, "text_merged_paths": text_merged, "conflicts": []},
    )


def cmd_conflicts_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    clauses = []
    params: list[Any] = []
    if args.workspace:
        clauses.append("workspace_id = ?")
        params.append(args.workspace)
    if args.status:
        clauses.append("status = ?")
        params.append(args.status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.rows(f"SELECT * FROM workspace_conflicts {where} ORDER BY created_at DESC", tuple(params))
    conflicts = []
    redacted = 0
    for row in rows:
        decision, _ = object_access_decision(store, row["workspace_id"], actor=args.actor, action="workspace.read")
        if decision["allowed"]:
            conflicts.append(workspace_conflict_data(store, row))
        else:
            redacted += 1
    return envelope("tri.conflicts.list", command, actor=args.actor, start=start, data={"conflicts": conflicts, "redacted": redacted})


def cmd_conflicts_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = store.get_workspace_conflict(args.conflict)
    require_object_access(store, row["workspace_id"], actor=args.actor, action="workspace.read")
    return envelope("tri.conflicts.inspect", command, actor=args.actor, start=start, data=workspace_conflict_data(store, row))


def choose_conflict_entry(
    strategy: str,
    path: str,
    *,
    base_entries: dict[str, dict[str, Any]],
    head_entries: dict[str, dict[str, Any]],
    target_entries: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if strategy == "ours":
        return head_entries.get(path)
    if strategy == "theirs":
        return target_entries.get(path)
    if strategy == "base":
        return base_entries.get(path)
    raise TriError("invalid_command", "Unknown conflict resolution strategy.", strategy=strategy)


def cmd_conflicts_resolve(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = store.get_workspace_conflict(args.conflict)
    if row["status"] != "open":
        raise TriError("policy_denied", "Only open conflicts can be resolved.", object=row["id"], status=row["status"])
    ws = store.get_workspace(row["workspace_id"])
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can resolve conflicts.", object=ws["id"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    if ws["head_snapshot_id"] != row["head_snapshot_id"]:
        raise TriError("stale_workspace_head", "Workspace head moved after conflict was recorded.", object=ws["id"], expected=row["head_snapshot_id"], actual=ws["head_snapshot_id"], retryable=True)
    base_entries = store.load_snapshot_tree(row["base_snapshot_id"])
    head_entries = store.load_snapshot_tree(row["head_snapshot_id"])
    target_entries = store.load_snapshot_tree(row["target_snapshot_id"])
    merged_entries, conflicts, _ = merge_entries(store, base_entries, head_entries, target_entries)
    conflict_paths = [item["path"] for item in loads(row["conflicts_json"], [])]
    if conflicts:
        still_conflicting = {item["path"] for item in conflicts}
        missing = [path for path in conflict_paths if path not in still_conflicting]
        if missing:
            raise TriError("stale_workspace_head", "Conflict inputs no longer match recorded conflict paths.", object=row["id"], paths=missing, retryable=True)
    for path in conflict_paths:
        chosen = choose_conflict_entry(args.strategy, path, base_entries=base_entries, head_entries=head_entries, target_entries=target_entries)
        if chosen is None:
            merged_entries.pop(path, None)
        else:
            merged_entries[path] = chosen
    merged_entries = dict(sorted(merged_entries.items()))
    changed = changed_paths(head_entries, merged_entries)
    with store.conn:
        snapshot = store.create_snapshot(
            ws["project_id"],
            merged_entries,
            parents=[ws["head_snapshot_id"], row["target_snapshot_id"]],
            source="merge",
            actor=args.actor,
            metadata={"workspace": ws["id"], "operation": "resolve_conflict", "conflict": row["id"], "strategy": args.strategy},
        )
        op_id = new_id("op")
        store.conn.execute(
            "INSERT INTO operations (id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id, path_set_json, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, ws["id"], args.actor, "resolve_conflict", ws["head_snapshot_id"], snapshot["id"], dumps(changed), dumps({"conflict": row["id"], "strategy": args.strategy, "target_snapshot": row["target_snapshot_id"]}), utcnow()),
        )
        updated = store.conn.execute(
            "UPDATE workspaces SET base_snapshot_id = ?, head_snapshot_id = ?, updated_at = ? WHERE id = ? AND head_snapshot_id = ?",
            (row["target_snapshot_id"], snapshot["id"], utcnow(), ws["id"], ws["head_snapshot_id"]),
        )
        if updated.rowcount != 1:
            raise_stale_workspace_head(store, ws["id"], ws["head_snapshot_id"], "Workspace head changed before conflict resolution could commit.")
        resolution = {"strategy": args.strategy, "operation": op_id, "snapshot": snapshot["id"], "paths": changed}
        store.conn.execute(
            "UPDATE workspace_conflicts SET status = ?, resolution_json = ?, resolved_at = ? WHERE id = ?",
            ("resolved", dumps(resolution), utcnow(), row["id"]),
        )
        evt = store.create_event(
            kind="workspace.conflict_resolved",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"conflict": row["id"], "operation": op_id, "snapshot": snapshot["id"], "strategy": args.strategy, "paths": changed},
            input_fingerprints={"head_snapshot": ws["head_snapshot_id"], "target_snapshot": row["target_snapshot_id"]},
            output_fingerprints={"head_snapshot": snapshot["id"]},
        )
    return envelope(
        "tri.conflicts.resolve",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={"conflict": row["id"], "workspace": ws["id"], "operation": op_id, "strategy": args.strategy, "snapshot": snapshot["id"], "base_snapshot": row["target_snapshot_id"], "changed_paths": changed},
    )


def cmd_workspace_sync(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can sync materialized edits.", object=ws["id"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    lease = resolve_materialization_lease(store, ws["id"], args.lease, actor=args.actor)
    require_active_materialization_lease(store, lease)
    if lease["last_sync_snapshot_id"] != ws["head_snapshot_id"]:
        raise TriError(
            "stale_materialization",
            "Workspace head moved after materialization; remount or resolve before syncing.",
            object=ws["id"],
            lease=lease["id"],
            expected=lease["last_sync_snapshot_id"],
            actual=ws["head_snapshot_id"],
            retryable=True,
        )
    root = Path(lease["path"])
    if not root.exists():
        raise TriError("stale_materialization", "Materialized path no longer exists.", object=str(root), lease=lease["id"])
    before = ws["head_snapshot_id"]
    before_entries = store.load_snapshot_tree(before)
    ignored_paths = loads(lease["ignored_paths_json"], [])
    if root.is_symlink():
        raise TriError("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(root))
    baseline_file = Path(row_value(lease, "baseline_path") or "") if row_value(lease, "baseline_path") else None
    materialization: Materialization | None = None
    observed: dict[str, list[int]] = {}
    scanned_deleted: list[str] = []
    if baseline_file is not None and baseline_file.exists():
        materialization = load_materialization(root, baseline_file)
        rules = import_rules(store, ws["project_id"], before_entries)
        scanned_changed, scanned_deleted = materialization.scan(sparse_entries(before_entries, lease_sparse_paths(lease)), ignore=rules)
        scanned_changed = [path for path in scanned_changed if not path_matches_hidden(path, ignored_paths)]
        scanned_deleted = [path for path in scanned_deleted if not path_matches_hidden(path, ignored_paths)]
        after_entries = entries_after_scan(store, root, before_entries, scanned_changed, scanned_deleted, observed)
    else:
        synced_entries = filter_ignored_entries(build_entries_from_dir(store, root, allow_symlinks=True), ignored_paths)
        sparse = lease_sparse_paths(lease)
        after_entries = {path: entry for path, entry in before_entries.items() if path_matches_hidden(path, ignored_paths) or (sparse and not path_matches_filters(path, sparse))}
        after_entries.update(synced_entries)
    paths = changed_paths(before_entries, after_entries)

    def record_sync_baseline() -> None:
        if materialization is not None and baseline_file is not None:
            materialization.update_baseline(observed, scanned_deleted)
            materialization.save_baseline(baseline_file)
        renew_mount_lease(store, lease["id"])

    if not paths:
        record_sync_baseline()
        return envelope("tri.workspace.sync", command, actor=args.actor, start=start, data={"workspace": ws["id"], "lease": lease["id"], "operation": None, "snapshot": before, "paths": []})
    with store.conn:
        snapshot = store.create_snapshot(ws["project_id"], after_entries, parents=[before], source="workspace", actor=args.actor, metadata={"workspace": ws["id"], "operation": "materialization_sync"})
        op_id = new_id("op")
        store.conn.execute(
            "INSERT INTO operations (id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id, path_set_json, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, ws["id"], args.actor, "materialization_sync", before, snapshot["id"], dumps(paths), dumps({"path": str(root)}), utcnow()),
        )
        updated = store.conn.execute(
            "UPDATE workspaces SET head_snapshot_id = ?, materialized_snapshot_id = ?, updated_at = ? WHERE id = ? AND head_snapshot_id = ?",
            (snapshot["id"], snapshot["id"], utcnow(), ws["id"], before),
        )
        if updated.rowcount != 1:
            current = store.row("SELECT head_snapshot_id FROM workspaces WHERE id = ?", (ws["id"],))
            raise TriError(
                "stale_materialization",
                "Workspace head moved while syncing materialized edits.",
                object=ws["id"],
                lease=lease["id"],
                expected=before,
                actual=current["head_snapshot_id"] if current else None,
                retryable=True,
            )
        store.conn.execute("UPDATE materialization_leases SET last_sync_snapshot_id = ? WHERE id = ?", (snapshot["id"], lease["id"]))
        evt = store.create_event(
            kind="workspace.materialization_sync",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"operation": op_id, "snapshot": snapshot["id"], "paths": paths, "lease": lease["id"]},
            input_fingerprints={"snapshot": before, "lease": lease["last_sync_snapshot_id"]},
            output_fingerprints={"snapshot": snapshot["id"]},
        )
    record_sync_baseline()
    return envelope("tri.workspace.sync", command, actor=args.actor, start=start, events=[evt], data={"workspace": ws["id"], "lease": lease["id"], "operation": op_id, "snapshot": snapshot["id"], "paths": paths})


def cmd_files_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    snapshot, hidden_patterns = resolve_ref_context(store, args.ref, actor=args.actor, action="workspace.read")
    entries = store.load_snapshot_tree(snapshot)
    files = []
    redacted_skipped = 0
    policy_denied_skipped = 0
    for path, entry in entries.items():
        visible, reason = path_visibility(path, hidden_patterns)
        if not visible:
            redacted_skipped += 1
            if reason == "hidden_path":
                policy_denied_skipped += 1
            continue
        files.append({"path": path, **entry})
    return envelope(
        "tri.files.list",
        command,
        actor=args.actor,
        start=start,
        data={"snapshot": snapshot, "files": files, "redacted_skipped": redacted_skipped, "policy_denied_skipped": policy_denied_skipped},
    )


def resolve_snapshot_ref(store: Store, ref: str) -> str:
    if ref.startswith("ws_"):
        ws = store.get_workspace(ref)
        return ws["head_snapshot_id"]
    if ref.startswith("snap_"):
        snapshot = store.row("SELECT id FROM snapshots WHERE id = ?", (ref,))
        if snapshot is None:
            raise TriError("path_not_found", "Snapshot not found.", object=ref)
        return ref
    raise TriError("path_not_found", "Expected a workspace or snapshot id.", object=ref)


def snapshot_row(store: Store, snapshot: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM snapshots WHERE id = ?", (snapshot,))
    if row is None:
        raise TriError("path_not_found", "Snapshot not found.", object=snapshot)
    return row


def snapshot_access_candidates(store: Store, snapshot: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(object_id: Any, action: str) -> None:
        if not object_id:
            return
        key = (str(object_id), action)
        if key in seen:
            return
        seen.add(key)
        candidates.append(key)

    row = snapshot_row(store, snapshot)
    metadata = loads(row["metadata_json"], {})
    add(metadata.get("workspace"), "workspace.read")
    add(metadata.get("changeset"), "changeset.read")
    for workspace in store.rows(
        """
        SELECT DISTINCT workspace_id FROM operations
        WHERE input_snapshot_id = ? OR output_snapshot_id = ?
        ORDER BY workspace_id
        """,
        (snapshot, snapshot),
    ):
        add(workspace["workspace_id"], "workspace.read")
    for workspace in store.rows("SELECT id FROM workspaces WHERE head_snapshot_id = ? OR materialized_snapshot_id = ? ORDER BY id", (snapshot, snapshot)):
        add(workspace["id"], "workspace.read")
    for changeset in store.rows("SELECT id FROM changesets WHERE head_snapshot_id = ? ORDER BY id", (snapshot,)):
        add(changeset["id"], "changeset.read")
    for check in store.rows(
        """
        SELECT DISTINCT workspace_id, changeset_id FROM check_runs
        WHERE snapshot_before = ? OR snapshot_after = ?
        ORDER BY changeset_id, workspace_id
        """,
        (snapshot, snapshot),
    ):
        if check["changeset_id"]:
            add(check["changeset_id"], "changeset.read")
        elif check["workspace_id"]:
            add(check["workspace_id"], "workspace.read")
    return candidates


def snapshot_has_public_source(store: Store, snapshot: str) -> bool:
    row = snapshot_row(store, snapshot)
    metadata = loads(row["metadata_json"], {})
    if row["source"] == "imported":
        return True
    if row["source"] == "merge" and metadata.get("target"):
        return True
    if store.row("SELECT id FROM targets WHERE snapshot_id = ? LIMIT 1", (snapshot,)) is not None:
        return True
    if store.row("SELECT id FROM releases WHERE target_snapshot_id = ? LIMIT 1", (snapshot,)) is not None:
        return True
    return False


def hidden_patterns_for_snapshot(store: Store, snapshot: str, *, actor: str | None = None) -> list[str]:
    snapshot_record = snapshot_row(store, snapshot)
    if snapshot_has_public_source(store, snapshot):
        return effective_hidden_patterns(store, snapshot_record["project_id"], [], actor=actor)
    patterns: set[str] = set()
    for row in store.rows(
        "SELECT hidden_paths_json FROM workspaces WHERE head_snapshot_id = ? OR base_snapshot_id = ? OR materialized_snapshot_id = ?",
        (snapshot, snapshot, snapshot),
    ):
        patterns.update(loads(row["hidden_paths_json"], []))
    for row in store.rows(
        "SELECT hidden_paths_json FROM changesets WHERE head_snapshot_id = ? OR base_snapshot_id = ?",
        (snapshot, snapshot),
    ):
        patterns.update(loads(row["hidden_paths_json"], []))
    for row in store.rows(
        """
        SELECT DISTINCT workspaces.hidden_paths_json
        FROM operations
        JOIN workspaces ON workspaces.id = operations.workspace_id
        WHERE operations.input_snapshot_id = ? OR operations.output_snapshot_id = ?
        """,
        (snapshot, snapshot),
    ):
        patterns.update(loads(row["hidden_paths_json"], []))
    return effective_hidden_patterns(store, snapshot_record["project_id"], sorted(patterns), actor=actor)


def content_origin_access(
    *,
    object_id: str,
    actor: str,
    action: str,
    origins: list[dict[str, Any]],
    origin_count: int,
    redacted_origins: int,
) -> dict[str, Any]:
    return {
        "allowed": bool(origins),
        "actor": actor,
        "action": action,
        "object": object_id,
        "reason": "origin_access" if origins else "missing_allow",
        "origin_count": origin_count,
        "redacted_origins": redacted_origins,
    }


def snapshot_file_origin(snapshot: sqlite3.Row, path: str, entry: dict[str, Any], access: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "snapshot_file",
        "snapshot": snapshot["id"],
        "project": snapshot["project_id"],
        "path": path,
        "mode": entry.get("mode"),
        "size": entry.get("size"),
        "read_command": ["files", "read", snapshot["id"], path],
        "read_route": f"/v0/snapshots/{snapshot['id']}/files/{quote(path, safe='/')}",
        "access": {"reason": access["reason"], "origins": access["origins"]},
    }


def visible_blob_origins(store: Store, blob_id: str, *, actor: str) -> tuple[list[dict[str, Any]], int, int]:
    origins: list[dict[str, Any]] = []
    redacted_origins = 0
    origin_count = 0
    seen: set[tuple[str, str]] = set()
    for snapshot in store.rows("SELECT * FROM snapshots ORDER BY created_at, rowid"):
        entries = store.load_tree(snapshot["root_tree_id"])
        for path, entry in entries.items():
            if entry.get("blob") != blob_id:
                continue
            key = (snapshot["id"], path)
            if key in seen:
                continue
            seen.add(key)
            origin_count += 1
            access = snapshot_access_data(store, snapshot["id"], actor=actor)
            if not access["allowed"]:
                redacted_origins += 1
                continue
            visible, _ = path_visibility(path, hidden_patterns_for_snapshot(store, snapshot["id"], actor=actor))
            if not visible:
                redacted_origins += 1
                continue
            origins.append(snapshot_file_origin(snapshot, path, entry, access))
    return origins, origin_count, redacted_origins


def visible_tree_origins(store: Store, tree_id: str, *, actor: str) -> tuple[list[dict[str, Any]], int, int, dict[str, dict[str, Any]], int]:
    entries = store.load_tree(tree_id)
    origins: list[dict[str, Any]] = []
    redacted_origins = 0
    origin_count = 0
    visible_entries: dict[str, dict[str, Any]] = {}
    for snapshot in store.rows("SELECT * FROM snapshots WHERE root_tree_id = ? ORDER BY created_at, rowid", (tree_id,)):
        origin_count += 1
        access = snapshot_access_data(store, snapshot["id"], actor=actor)
        if not access["allowed"]:
            redacted_origins += 1
            continue
        hidden_patterns = hidden_patterns_for_snapshot(store, snapshot["id"], actor=actor)
        visible_count = 0
        for path, entry in entries.items():
            visible, _ = path_visibility(path, hidden_patterns)
            if visible:
                visible_count += 1
                visible_entries[path] = entry
        origins.append(
            {
                "type": "snapshot_tree",
                "snapshot": snapshot["id"],
                "project": snapshot["project_id"],
                "source": snapshot["source"],
                "visible_entries": visible_count,
                "redacted_entries": len(entries) - visible_count,
                "access": {"reason": access["reason"], "origins": access["origins"]},
            }
        )
    return origins, origin_count, redacted_origins, visible_entries, len(entries) - len(visible_entries)


def require_content_origins(object_id: str, *, action: str, origin_count: int, redacted_origins: int, origins: list[dict[str, Any]]) -> None:
    if origins:
        return
    raise TriError(
        "policy_denied",
        "Content object is not attached to a readable, visible origin.",
        object=object_id,
        action=action,
        origin_count=origin_count,
        redacted_origins=redacted_origins,
        required_grants=["snapshot.read"],
    )


def blob_inspect_data(store: Store, blob_id: str, *, actor: str) -> dict[str, Any]:
    row = blob_row(store, blob_id)
    origins, origin_count, redacted_origins = visible_blob_origins(store, blob_id, actor=actor)
    require_content_origins(blob_id, action="blob.inspect", origin_count=origin_count, redacted_origins=redacted_origins, origins=origins)
    return {
        "id": row["id"],
        "hash": row["hash"],
        "fingerprint": f"sha256:{row['hash']}",
        "size": row["size"],
        "binary": blob_is_binary(store, blob_id),
        "origins": origins,
        "origin_count": origin_count,
        "redacted_origins": redacted_origins,
        "access": content_origin_access(
            object_id=blob_id,
            actor=actor,
            action="blob.inspect",
            origins=origins,
            origin_count=origin_count,
            redacted_origins=redacted_origins,
        ),
    }


def tree_inspect_data(store: Store, tree_id: str, *, actor: str) -> dict[str, Any]:
    row = tree_row(store, tree_id)
    origins, origin_count, redacted_origins, visible_entries, redacted_entries = visible_tree_origins(store, tree_id, actor=actor)
    require_content_origins(tree_id, action="tree.inspect", origin_count=origin_count, redacted_origins=redacted_origins, origins=origins)
    entries = [{"path": path, **entry} for path, entry in sorted(visible_entries.items())]
    return {
        "id": row["id"],
        "hash": row["hash"],
        "fingerprint": f"sha256:{row['hash']}",
        "entries": entries,
        "file_count": len(entries),
        "byte_count": sum(int(entry.get("size", 0)) for entry in entries),
        "redacted_entries": redacted_entries,
        "origins": origins,
        "origin_count": origin_count,
        "redacted_origins": redacted_origins,
        "access": content_origin_access(
            object_id=tree_id,
            actor=actor,
            action="tree.inspect",
            origins=origins,
            origin_count=origin_count,
            redacted_origins=redacted_origins,
        ),
    }


def cmd_blobs_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.blob.inspect", command, actor=args.actor, start=start, data=blob_inspect_data(store, args.blob, actor=args.actor))


def cmd_trees_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.tree.inspect", command, actor=args.actor, start=start, data=tree_inspect_data(store, args.tree, actor=args.actor))


def require_snapshot_access(store: Store, snapshot: str, *, actor: str, action: str) -> None:
    if snapshot_has_public_source(store, snapshot):
        return
    candidates = snapshot_access_candidates(store, snapshot)
    if not candidates:
        raise TriError("policy_denied", "Snapshot is not attached to a readable object.", object=snapshot, action=action, candidate_count=0, redacted_candidates=0)
    decisions = []
    for object_id, object_action in candidates:
        decision, _ = object_access_decision(store, object_id, actor=actor, action=object_action)
        decisions.append(decision)
        if decision["allowed"]:
            return
    required_actions = sorted({decision["action"] for decision in decisions})
    raise TriError(
        "policy_denied",
        "Actor is not allowed to access this snapshot.",
        object=snapshot,
        action=action,
        candidate_count=len(decisions),
        redacted_candidates=len(decisions),
        required_grants=required_actions,
    )


def resolve_ref_context(store: Store, ref: str, *, actor: str | None = None, action: str = "workspace.read") -> tuple[str, list[str]]:
    if ref.startswith("ws_"):
        ws = store.get_workspace(ref)
        if actor:
            require_object_access(store, ws["id"], actor=actor, action=action)
        return ws["head_snapshot_id"], effective_hidden_patterns(store, ws["project_id"], loads(ws["hidden_paths_json"], []), actor=actor)
    if ref.startswith("cs_"):
        cs = store.get_changeset(ref)
        if actor:
            require_object_access(store, cs["id"], actor=actor, action="changeset.read")
        return cs["head_snapshot_id"], effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=actor)
    snapshot = resolve_snapshot_ref(store, ref)
    if actor:
        require_snapshot_access(store, snapshot, actor=actor, action=action)
    return snapshot, hidden_patterns_for_snapshot(store, snapshot, actor=actor)


def file_stat_data(store: Store, snapshot: str, path: str, hidden_patterns: list[str] | None = None) -> dict[str, Any]:
    entries = store.load_snapshot_tree(snapshot)
    entry = entries.get(path)
    if entry is None:
        raise TriError("path_not_found", "Path not found.", object=path)
    visible, reason = path_visibility(path, hidden_patterns)
    return {
        "snapshot": snapshot,
        "path": path,
        "exists": True,
        "type": "symlink" if entry.get("mode") == "120000" else "file",
        "blob": entry["blob"] if visible else None,
        "hash": entry["hash"] if visible else None,
        "size": entry["size"] if visible else None,
        "mode": entry["mode"] if visible else None,
        "symlink_target": entry.get("symlink_target") if visible else None,
        "visible": visible,
        "redaction": reason,
    }


def cmd_files_stat(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    snapshot, hidden_patterns = resolve_ref_context(store, args.ref, actor=args.actor, action="workspace.read")
    path = normalize_path(args.path)
    return envelope("tri.files.stat", command, actor=args.actor, start=start, data=file_stat_data(store, snapshot, path, hidden_patterns))


HISTORY_MAX_WALK = 5000


def resolve_history_ref(store: Store, ref: str, *, actor: str) -> tuple[str, list[str]]:
    if ref.startswith("target_"):
        row = store.row("SELECT * FROM targets WHERE id = ?", (ref,))
        if row is None:
            raise TriError("path_not_found", "Target not found.", object=ref)
        require_snapshot_access(store, row["snapshot_id"], actor=actor, action="snapshot.read")
        return row["snapshot_id"], hidden_patterns_for_snapshot(store, row["snapshot_id"], actor=actor)
    return resolve_ref_context(store, ref, actor=actor, action="workspace.read")


def snapshot_parents(store: Store, snapshot: str) -> list[str]:
    return loads(snapshot_row(store, snapshot)["parent_ids_json"], [])


def snapshot_readable(store: Store, snapshot: str, *, actor: str) -> bool:
    try:
        require_snapshot_access(store, snapshot, actor=actor, action="snapshot.read")
    except TriError:
        return False
    return True


def history_provenance(store: Store, snapshot: str) -> dict[str, Any]:
    row = snapshot_row(store, snapshot)
    metadata = loads(row["metadata_json"], {})
    data = {
        "snapshot": row["id"],
        "parents": loads(row["parent_ids_json"], []),
        "source": row["source"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
    for key in ("changeset", "workspace", "operation", "target", "git_commit"):
        if metadata.get(key):
            data[key] = metadata[key]
    return data


def first_parent_history(store: Store, snapshot: str, *, actor: str) -> tuple[list[str], dict[str, Any] | None]:
    """Snapshots along the first-parent chain, stopping at the first one the actor cannot read."""
    chain = [snapshot]
    truncated = None
    current = snapshot
    while len(chain) < HISTORY_MAX_WALK:
        parents = snapshot_parents(store, current)
        if not parents:
            return chain, None
        parent = parents[0]
        if not snapshot_readable(store, parent, actor=actor):
            return chain, {"reason": "policy_denied", "at": "redacted_snapshot"}
        chain.append(parent)
        current = parent
    if snapshot_parents(store, current):
        truncated = {"reason": "walk_limit", "limit": HISTORY_MAX_WALK}
    return chain, truncated


def require_visible_history_path(path: str, hidden_patterns: list[str]) -> None:
    visible, reason = path_visibility(path, hidden_patterns)
    if not visible:
        raise TriError("policy_denied", "Path is hidden by policy.", object=path, reason=reason)


def cmd_files_log(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    snapshot, hidden_patterns = resolve_history_ref(store, args.ref, actor=args.actor)
    path = normalize_path(args.path)
    require_visible_history_path(path, hidden_patterns)
    limit = max(1, args.limit)
    chain, truncated = first_parent_history(store, snapshot, actor=args.actor)
    entries: list[dict[str, Any]] = []
    for index, current in enumerate(chain):
        entry = store.load_snapshot_tree(current).get(path)
        parent_snapshot = chain[index + 1] if index + 1 < len(chain) else None
        if parent_snapshot is None and truncated is None:
            parent_entry = None
        elif parent_snapshot is None:
            break
        else:
            parent_entry = store.load_snapshot_tree(parent_snapshot).get(path)
        if entry == parent_entry:
            continue
        change = "added" if parent_entry is None else "deleted" if entry is None else "modified"
        entries.append({**history_provenance(store, current), "change": change, "blob": entry["blob"] if entry else None, "size": entry["size"] if entry else None})
        if len(entries) >= limit:
            break
    return envelope(
        "tri.files.log",
        command,
        actor=args.actor,
        start=start,
        data={"ref": args.ref, "snapshot": snapshot, "path": path, "entries": entries, "walked": len(chain), "truncated": truncated},
    )


def blob_lines(store: Store, entry: dict[str, Any] | None) -> list[bytes]:
    if entry is None:
        return []
    return store.read_blob(entry["blob"]).splitlines(keepends=True)


def cmd_files_blame(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    snapshot, hidden_patterns = resolve_history_ref(store, args.ref, actor=args.actor)
    path = normalize_path(args.path)
    require_visible_history_path(path, hidden_patterns)
    head_entry = store.load_snapshot_tree(snapshot).get(path)
    if head_entry is None:
        raise TriError("path_not_found", "Path not found.", object=path)
    if blob_is_binary(store, head_entry["blob"]):
        raise TriError("invalid_command", "Blame needs a text file.", object=path)
    chain, truncated = first_parent_history(store, snapshot, actor=args.actor)
    head_lines = blob_lines(store, head_entry)
    owner: list[str | None] = [None] * len(head_lines)
    # pending maps a head line index to its index in the version being examined.
    pending = {index: index for index in range(len(head_lines))}
    current_entry = head_entry
    current_lines = head_lines
    boundary = None
    for index, current in enumerate(chain):
        if not pending:
            break
        if index + 1 >= len(chain):
            # Lines that survive to the root were introduced there. When the walk
            # stopped at an unreadable snapshot, their origin stays redacted.
            if truncated is None:
                boundary = current
                for line in pending:
                    owner[line] = current
            pending = {}
            break
        parent_entry = store.load_snapshot_tree(chain[index + 1]).get(path)
        if parent_entry == current_entry:
            continue
        parent_lines = blob_lines(store, parent_entry)
        mapping: dict[int, int] = {}
        matcher = difflib.SequenceMatcher(None, parent_lines, current_lines, autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                mapping[block.b + offset] = block.a + offset
        still_pending = {}
        for line, position in pending.items():
            if position in mapping:
                still_pending[line] = mapping[position]
            else:
                owner[line] = current
        pending = still_pending
        current_entry = parent_entry
        current_lines = parent_lines
    ranges: list[dict[str, Any]] = []
    provenance: dict[str, dict[str, Any]] = {}
    for line, snap in enumerate(owner, start=1):
        if ranges and ranges[-1]["snapshot"] == snap and ranges[-1]["end"] == line - 1:
            ranges[-1]["end"] = line
            continue
        ranges.append({"start": line, "end": line, "snapshot": snap})
        if snap is not None and snap not in provenance:
            provenance[snap] = history_provenance(store, snap)
    for item in ranges:
        if item["snapshot"] is not None:
            details = provenance[item["snapshot"]]
            item.update({key: details[key] for key in ("created_by", "created_at", "source", "changeset", "git_commit") if key in details})
        else:
            item["redacted"] = True
    return envelope(
        "tri.files.blame",
        command,
        actor=args.actor,
        start=start,
        data={"ref": args.ref, "snapshot": snapshot, "path": path, "line_count": len(head_lines), "ranges": ranges, "boundary": boundary, "walked": len(chain), "truncated": truncated},
    )


def snapshot_ancestor_distances(store: Store, snapshot: str) -> dict[str, int]:
    distances = {snapshot: 0}
    frontier = [snapshot]
    while frontier and len(distances) < HISTORY_MAX_WALK * 4:
        next_frontier = []
        for current in frontier:
            for parent in snapshot_parents(store, current):
                if parent not in distances:
                    distances[parent] = distances[current] + 1
                    next_frontier.append(parent)
        frontier = next_frontier
    return distances


def cmd_snapshots_merge_base(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    left, _ = resolve_history_ref(store, args.left, actor=args.actor)
    right, _ = resolve_history_ref(store, args.right, actor=args.actor)
    left_distances = snapshot_ancestor_distances(store, left)
    right_distances = snapshot_ancestor_distances(store, right)
    common = [snap for snap in left_distances if snap in right_distances]
    base = min(common, key=lambda snap: (left_distances[snap] + right_distances[snap], left_distances[snap]), default=None)
    redacted = False
    if base is not None and not snapshot_readable(store, base, actor=args.actor):
        base = None
        redacted = True
    data: dict[str, Any] = {"left": left, "right": right, "merge_base": base, "redacted": redacted}
    if base is not None:
        data.update({"left_distance": left_distances[base], "right_distance": right_distances[base], "provenance": history_provenance(store, base)})
    return envelope("tri.snapshot.merge_base", command, actor=args.actor, start=start, data=data)


def cmd_files_read(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    snapshot, hidden_patterns = resolve_ref_context(store, args.ref, actor=args.actor, action="workspace.read")
    path = normalize_path(args.path)
    entries = store.load_snapshot_tree(snapshot)
    entry = entries.get(path)
    if entry is None:
        raise TriError("path_not_found", "Path not found.", object=path)
    visible, reason = path_visibility(path, hidden_patterns)
    if not visible:
        raise TriError("policy_denied", "Path is hidden by policy.", object=path, reason=reason)
    data = store.read_blob(entry["blob"])
    offset = max(0, getattr(args, "offset", 0) or 0)
    limit = getattr(args, "limit", None)
    if limit is not None:
        limit = max(0, limit)
        chunk = data[offset : offset + limit]
        eof = offset + len(chunk) >= len(data)
    else:
        chunk = data[offset:]
        eof = True
    try:
        text = chunk.decode("utf-8")
        payload = {"encoding": "utf-8", "text": text}
    except UnicodeDecodeError:
        payload = {"encoding": "base64", "text": base64.b64encode(chunk).decode()}
    return envelope(
        "tri.files.read",
        command,
        actor=args.actor,
        start=start,
        data={"snapshot": snapshot, "path": path, "blob": entry["blob"], "offset": offset, "limit": limit, "total_bytes": len(data), "eof": eof, **payload},
    )


FILES_FETCH_MAX_BYTES = 16 * 1024 * 1024


def cmd_files_fetch(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    """Read several files in one call (remote mounts and caches); same policy as files read."""
    snapshot, hidden_patterns = resolve_ref_context(store, args.ref, actor=args.actor, action="workspace.read")
    entries = store.load_snapshot_tree(snapshot)
    budget = min(args.max_bytes or FILES_FETCH_MAX_BYTES, FILES_FETCH_MAX_BYTES)
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    deferred: list[str] = []
    used = 0
    for raw in args.path or []:
        path = normalize_path(raw)
        entry = entries.get(path)
        if entry is None:
            missing.append(path)
            continue
        visible, reason = path_visibility(path, hidden_patterns)
        if not visible:
            raise TriError("policy_denied", "Path is hidden by policy.", object=path, reason=reason)
        if files and used + int(entry.get("size") or 0) > budget:
            deferred.append(path)
            continue
        data = store.read_blob(entry["blob"])
        used += len(data)
        files.append({"path": path, "hash": entry["hash"], "mode": entry.get("mode", "100644"), "size": len(data), "encoding": "base64", "content": base64.b64encode(data).decode()})
    return envelope("tri.files.fetch", command, actor=args.actor, start=start, data={"snapshot": snapshot, "files": files, "missing": missing, "deferred": deferred, "bytes": used})


def cmd_files_write(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    path = normalize_path(args.path)
    if args.body:
        data = Path(args.body).read_bytes()
    elif getattr(args, "base64", None) is not None:
        try:
            data = base64.b64decode(args.base64, validate=True)
        except ValueError:
            raise TriError("invalid_command", "--base64 is not valid base64.") from None
    else:
        data = (args.text or "").encode()

    def apply(entries: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        blob = store.put_blob(data)
        entries[path] = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": "100644"}
        return dict(sorted(entries.items())), [path]

    result, events = mutate_workspace(
        store,
        args.workspace,
        actor=args.actor,
        kind="write_file",
        apply_change=apply,
        metadata={"path": path},
        expect_head=args.expect_head,
    )
    return envelope("tri.files.write", command, actor=args.actor, start=start, events=events, data=result)


def cmd_files_search(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    snapshot, hidden_patterns = resolve_ref_context(store, args.ref, actor=args.actor, action="workspace.read")
    entries = store.load_snapshot_tree(snapshot)
    query = args.query if args.case_sensitive else args.query.lower()
    path_glob = args.glob or "*"
    limit = max(1, min(args.limit, 1000))
    matches: list[dict[str, Any]] = []
    redacted_skipped = 0
    policy_denied_skipped = 0
    binary_skipped = 0
    for path, entry in entries.items():
        if not fnmatch.fnmatch(path, path_glob):
            continue
        visible, reason = path_visibility(path, hidden_patterns)
        if not visible:
            redacted_skipped += 1
            if reason == "hidden_path":
                policy_denied_skipped += 1
            continue
        raw = store.read_blob(entry["blob"])
        if b"\0" in raw:
            binary_skipped += 1
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            haystack = line if args.case_sensitive else line.lower()
            column = haystack.find(query)
            if column == -1:
                continue
            matches.append(
                {
                    "path": path,
                    "line": line_number,
                    "column": column + 1,
                    "text": line,
                    "access": search_access_summary(None, object_id=args.ref, action="files.search", reason="authorized_snapshot_context"),
                }
            )
            if len(matches) >= limit:
                return envelope(
                    "tri.files.search",
                    command,
                    actor=args.actor,
                    start=start,
                    data={
                        "snapshot": snapshot,
                        "query": args.query,
                        "glob": path_glob,
                        "matches": matches,
                        "redacted_skipped": redacted_skipped,
                        "policy_denied_skipped": policy_denied_skipped,
                        "binary_skipped": binary_skipped,
                        "truncated": True,
                    },
                )
    return envelope(
        "tri.files.search",
        command,
        actor=args.actor,
        start=start,
        data={
            "snapshot": snapshot,
            "query": args.query,
            "glob": path_glob,
            "matches": matches,
            "redacted_skipped": redacted_skipped,
            "policy_denied_skipped": policy_denied_skipped,
            "binary_skipped": binary_skipped,
            "truncated": False,
        },
    )


SEARCH_SCOPES = ["targets", "packages", "workspaces", "changesets", "tasks", "reviews", "checks", "releases"]


def resolve_search_scopes(scopes: list[str] | None) -> list[str]:
    if not scopes or "all" in scopes:
        return list(SEARCH_SCOPES)
    resolved = []
    for scope in scopes:
        if scope not in SEARCH_SCOPES:
            raise TriError("invalid_command", "Unknown search scope.", object=scope)
        if scope not in resolved:
                resolved.append(scope)
    return resolved


def search_access_summary(
    decision: dict[str, Any] | None,
    *,
    object_id: str,
    action: str,
    reason: str | None = None,
    grants: list[str] | None = None,
) -> dict[str, Any]:
    summary = {
        "allowed": True,
        "object": object_id,
        "action": action,
        "reason_code": (decision or {}).get("reason_code", "allowed"),
        "reason": reason or (decision or {}).get("reason", "visible"),
        "grants": sorted(set(grants if grants is not None else (decision or {}).get("grants", []))),
        "matched_grants": (decision or {}).get("matched_grants", []),
    }
    policy_object = (decision or {}).get("object")
    policy_action = (decision or {}).get("action")
    if policy_object and policy_object != object_id:
        summary["policy_object"] = policy_object
    if policy_action and policy_action != action:
        summary["policy_action"] = policy_action
    return summary


def append_text_matches(
    matches: list[dict[str, Any]],
    *,
    query: str,
    case_sensitive: bool,
    limit: int,
    scope: str,
    object_type: str,
    object_id: str,
    project_id: str,
    field: str,
    text: str,
    provenance: dict[str, Any],
    path: str | None = None,
    snapshot: str | None = None,
    access: dict[str, Any] | None = None,
) -> bool:
    needle = query if case_sensitive else query.lower()
    for line_number, line in enumerate(text.splitlines() or [text], start=1):
        haystack = line if case_sensitive else line.lower()
        column = haystack.find(needle)
        if column == -1:
            continue
        matches.append(
            {
                "scope": scope,
                "object_type": object_type,
                "object": object_id,
                "project": project_id,
                "field": field,
                "path": path,
                "snapshot": snapshot,
                "line": line_number,
                "column": column + 1,
                "text": line,
                "provenance": provenance,
                "access": access or search_access_summary(None, object_id=object_id, action=f"{object_type}.read"),
            }
        )
        if len(matches) >= limit:
            return True
    return False


def append_json_field_matches(
    matches: list[dict[str, Any]],
    *,
    query: str,
    case_sensitive: bool,
    limit: int,
    scope: str,
    object_type: str,
    object_id: str,
    project_id: str,
    field: str,
    value: Any,
    provenance: dict[str, Any],
    access: dict[str, Any] | None = None,
) -> bool:
    if value in (None, "", [], {}):
        return False
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return append_text_matches(
        matches,
        query=query,
        case_sensitive=case_sensitive,
        limit=limit,
        scope=scope,
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        field=field,
        text=text,
        provenance=provenance,
        access=access,
    )


def append_snapshot_matches(
    store: Store,
    matches: list[dict[str, Any]],
    counters: dict[str, int],
    *,
    query: str,
    case_sensitive: bool,
    limit: int,
    project_id: str,
    snapshot: str,
    hidden_patterns: list[str],
    path_glob: str,
    scope: str,
    object_type: str,
    object_id: str,
    provenance: dict[str, Any],
    access: dict[str, Any] | None = None,
) -> bool:
    counters["snapshots_searched"] += 1
    entries = store.load_snapshot_tree(snapshot)
    for path, entry in entries.items():
        if not fnmatch.fnmatch(path, path_glob):
            continue
        visible, reason = path_visibility(path, hidden_patterns)
        if not visible:
            counters["redacted_skipped"] += 1
            if reason == "hidden_path":
                counters["policy_denied_skipped"] += 1
            continue
        raw = store.read_blob(entry["blob"])
        if b"\0" in raw:
            counters["binary_skipped"] += 1
            continue
        text = raw.decode("utf-8", errors="replace")
        if append_text_matches(
            matches,
            query=query,
            case_sensitive=case_sensitive,
            limit=limit,
            scope=scope,
            object_type=object_type,
            object_id=object_id,
            project_id=project_id,
            field="content",
            text=text,
            provenance=provenance,
            path=path,
            snapshot=snapshot,
            access=access,
        ):
            return True
    return False


def append_artifact_matches(
    store: Store,
    matches: list[dict[str, Any]],
    counters: dict[str, int],
    *,
    artifact_id: str,
    query: str,
    case_sensitive: bool,
    limit: int,
    project_id: str,
    scope: str,
    object_type: str,
    object_id: str,
    field: str,
    provenance: dict[str, Any],
    actor: str,
    access: dict[str, Any] | None = None,
) -> bool:
    decision, diagnostics = artifact_access_decision(store, artifact_id, actor=actor)
    if diagnostics:
        counters["policy_denied_skipped"] += 1
        return False
    row = store.get_artifact(artifact_id)
    raw = Path(row["path"]).read_bytes()
    if b"\0" in raw:
        counters["binary_skipped"] += 1
        return False
    text = raw.decode("utf-8", errors="replace")
    counters["artifacts_searched"] += 1
    return append_text_matches(
        matches,
        query=query,
        case_sensitive=case_sensitive,
        limit=limit,
        scope=scope,
        object_type=object_type,
        object_id=object_id,
        project_id=project_id,
        field=field,
        text=text,
        provenance={**provenance, "artifact": artifact_id, "artifact_kind": row["kind"]},
        access=access or search_access_summary(decision, object_id=artifact_id, action="artifact.read"),
    )


def linked_changesets_for_check(store: Store, check_id: str) -> list[str]:
    changesets = []
    for row in store.rows("SELECT id, checks_json FROM changesets WHERE checks_json LIKE ? ORDER BY created_at, rowid", (f"%{check_id}%",)):
        if check_id in loads(row["checks_json"], []):
            changesets.append(row["id"])
    return changesets


def linked_changesets_for_operation(store: Store, operation_id: str) -> list[str]:
    changesets = []
    for row in store.rows("SELECT id, operations_json FROM changesets WHERE operations_json LIKE ? ORDER BY created_at, rowid", (f"%{operation_id}%",)):
        if operation_id in loads(row["operations_json"], []):
            changesets.append(row["id"])
    return changesets


def check_access_decision(store: Store, check: dict[str, Any], *, actor: str) -> dict[str, Any]:
    if check["actor"] == actor:
        return {
            "allowed": True,
            "reason": "check_actor",
            "object": check["id"],
            "actor": actor,
            "action": "check.read",
            "grants": ["check.actor"],
            "denials": [],
        }
    if check["changeset"]:
        decision, _ = object_access_decision(store, check["changeset"], actor=actor, action="changeset.read")
        return decision
    for changeset_id in linked_changesets_for_check(store, check["id"]):
        decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
        if decision["allowed"]:
            return {**decision, "reason": "linked_changeset"}
    if check["workspace"]:
        decision, _ = object_access_decision(store, check["workspace"], actor=actor, action="workspace.read")
        return decision
    return {
        "allowed": True,
        "reason": "unscoped_check",
        "object": check["id"],
        "actor": actor,
        "action": "check.read",
        "grants": [],
        "denials": [],
    }


def require_check_access(store: Store, check: dict[str, Any], *, actor: str) -> dict[str, Any]:
    decision = check_access_decision(store, check, actor=actor)
    if not decision["allowed"]:
        denial = check_policy_denial_decision(check, decision, actor=actor)
        ctx = check_event_context(store, check)
        evt = store.record_policy_decision(
            actor=actor,
            action="check.read",
            object_id=check["id"],
            decision=denial,
            project_id=ctx["project"],
            workspace_id=ctx["workspace"],
            task_id=ctx["task"],
            kind="policy.denied",
        )
        raise TriError(
            "policy_denied",
            "Actor is not allowed to access this check.",
            object=check["id"],
            action="check.read",
            required_grant="check.read",
            decision=denial["id"],
            event=evt,
            explain_command=["tri", "policy", "explain", denial["id"]],
        )
    return decision


def check_hidden_patterns(store: Store, check: dict[str, Any], *, actor: str | None = None) -> list[str]:
    patterns: list[str] = []
    project_id: str | None = None
    if check.get("changeset"):
        try:
            changeset = store.get_changeset(check["changeset"])
            project_id = changeset["project_id"]
            patterns.extend(loads(changeset["hidden_paths_json"], []))
        except TriError:
            pass
    for changeset_id in linked_changesets_for_check(store, check["id"]):
        try:
            changeset = store.get_changeset(changeset_id)
        except TriError:
            continue
        project_id = project_id or changeset["project_id"]
        patterns.extend(loads(changeset["hidden_paths_json"], []))
    if check.get("workspace"):
        try:
            workspace = store.get_workspace(check["workspace"])
            project_id = project_id or workspace["project_id"]
            patterns.extend(loads(workspace["hidden_paths_json"], []))
        except TriError:
            pass
    if project_id is None:
        return sorted(set(patterns))
    return effective_hidden_patterns(store, project_id, sorted(set(patterns)), actor=actor)


def redact_check_file_changes(value: Any, hidden_patterns: list[str], *, root_key: str | None = None) -> tuple[Any, int]:
    redacted = 0

    def visit(item: Any, *, key: str | None = None) -> Any:
        nonlocal redacted
        if isinstance(item, dict):
            return {item_key: visit(item_value, key=item_key) for item_key, item_value in item.items()}
        if isinstance(item, list):
            output = []
            for child in item:
                redacted_child = visit(child, key=key)
                if redacted_child is not None:
                    output.append(redacted_child)
            return output
        if isinstance(item, str) and key == "file_changes":
            visible, _ = path_visibility(item, hidden_patterns)
            if not visible:
                redacted += 1
                return None
        return item

    return visit(value, key=root_key), redacted


def check_file_change_redaction_count(store: Store, file_changes: list[Any], hidden_patterns: list[str]) -> int:
    redacted = 0
    for item in file_changes:
        if isinstance(item, str) and item.startswith("op_"):
            operation = operation_detail_data(store, item, hidden_patterns)
            if operation is not None:
                redacted += operation.get("redacted_paths", 0)
        elif isinstance(item, str):
            visible, _ = path_visibility(item, hidden_patterns)
            if not visible:
                redacted += 1
    return redacted


def check_view(store: Store, check_id: str, *, actor: str) -> dict[str, Any]:
    check = check_data(store, check_id)
    decision = require_check_access(store, check, actor=actor)
    visible_linked_changesets = []
    for changeset_id in linked_changesets_for_check(store, check_id):
        changeset_decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
        if changeset_decision["allowed"]:
            visible_linked_changesets.append(changeset_id)
    view = {**check, "linked_changesets": visible_linked_changesets, "access": {"allowed": True, "reason": decision.get("reason"), "grants": decision.get("grants", [])}}
    hidden_patterns = check_hidden_patterns(store, check, actor=actor)
    view["file_changes"], redacted_file_changes = redact_check_file_changes(view["file_changes"], hidden_patterns, root_key="file_changes")
    view["inputs"], redacted_inputs = redact_check_file_changes(view["inputs"], hidden_patterns)
    view["outputs"], redacted_outputs = redact_check_file_changes(view["outputs"], hidden_patterns)
    view["redacted_file_changes"] = max(
        redacted_file_changes,
        redacted_inputs,
        redacted_outputs,
        check_file_change_redaction_count(store, view["file_changes"], hidden_patterns),
    )
    if check["workspace"]:
        workspace_decision, _ = object_access_decision(store, check["workspace"], actor=actor, action="workspace.read")
        view["redacted_workspace"] = not workspace_decision["allowed"]
        if not workspace_decision["allowed"]:
            view["workspace"] = None
            if isinstance(view.get("inputs"), dict):
                view["inputs"] = {**view["inputs"], "workspace": None}
    else:
        view["redacted_workspace"] = False
    if check["changeset"]:
        changeset_decision, _ = object_access_decision(store, check["changeset"], actor=actor, action="changeset.read")
        view["redacted_changeset"] = not changeset_decision["allowed"]
        if not changeset_decision["allowed"]:
            view["changeset"] = None
            if isinstance(view.get("inputs"), dict):
                view["inputs"] = {**view["inputs"], "changeset": None}
    else:
        view["redacted_changeset"] = False
    return view


def json_contains_string(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return value == needle
    if isinstance(value, list):
        return any(json_contains_string(item, needle) for item in value)
    if isinstance(value, dict):
        return any(json_contains_string(item, needle) for item in value.values())
    return False


def check_project_id(store: Store, check: dict[str, Any]) -> str | None:
    if check["changeset"]:
        return store.get_changeset(check["changeset"])["project_id"]
    if check["workspace"]:
        return store.get_workspace(check["workspace"])["project_id"]
    return None


def check_event_context(store: Store, check: dict[str, Any]) -> dict[str, str | None]:
    project_id = check_project_id(store, check)
    workspace_id = check.get("workspace")
    task_id = None
    if check.get("changeset") and not workspace_id:
        try:
            workspace_id = store.get_changeset(check["changeset"])["workspace_id"]
        except TriError:
            workspace_id = None
    if check.get("task_run"):
        row = store.row("SELECT task_id FROM task_runs WHERE id = ?", (check["task_run"],))
        task_id = row["task_id"] if row else None
    if not task_id and workspace_id:
        try:
            task_id = store.get_workspace(workspace_id)["task_id"]
        except TriError:
            task_id = None
    return {"project": project_id, "workspace": workspace_id, "task": task_id}


def check_read_policy_decision(check: dict[str, Any], origin_decision: dict[str, Any], *, actor: str) -> dict[str, Any]:
    allowed = bool(origin_decision.get("allowed"))
    return {
        "id": new_id("decision"),
        "allowed": allowed,
        "actor": actor,
        "action": "check.read",
        "object": check["id"],
        "object_type": "check",
        "policies": ["local_policy_v0"],
        "visibility": ["derived"],
        "owner": check.get("actor"),
        "actor_selectors": actor_selectors(actor),
        "origin_decision": origin_decision,
        "grants": origin_decision.get("grants", []),
        "matched_grants": origin_decision.get("matched_grants", []),
        "denials": [] if allowed else ["policy_denied"],
        "redactions": [],
        "reason_code": "allowed" if allowed else "policy_denied",
        "reason": origin_decision.get("reason", "missing_allow"),
        "event": None,
    }


def check_policy_denial_decision(check: dict[str, Any], origin_decision: dict[str, Any], *, actor: str) -> dict[str, Any]:
    return check_read_policy_decision(check, origin_decision, actor=actor)


def artifact_origins(store: Store, artifact_id: str) -> list[dict[str, Any]]:
    origins: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add(origin: dict[str, Any]) -> None:
        key = (origin.get("type"), origin.get("id"), origin.get("field"), origin.get("policy_object"))
        if key in seen:
            return
        seen.add(key)
        origins.append(origin)

    for row in store.rows("SELECT * FROM check_runs WHERE logs_json LIKE ? OR artifacts_json LIKE ? ORDER BY created_at, rowid", (f"%{artifact_id}%", f"%{artifact_id}%")):
        check = check_data(store, row["id"])
        fields = []
        for log_name, log_artifact in check["logs"].items():
            if log_artifact == artifact_id:
                fields.append(f"log.{log_name}")
        if artifact_id in check["artifacts"]:
            fields.append("artifacts")
        for field in sorted(set(fields)):
            policy_object = check["changeset"] or check["workspace"]
            policy_action = "changeset.read" if check["changeset"] else "workspace.read"
            add(
                {
                    "type": "check",
                    "id": check["id"],
                    "field": field,
                    "project": check_project_id(store, check),
                    "workspace": check["workspace"],
                    "changeset": check["changeset"],
                    "policy_object": policy_object,
                    "policy_action": policy_action if policy_object else None,
                    "created_at": row["created_at"],
                }
            )
            if not check["changeset"]:
                for changeset_id in linked_changesets_for_check(store, check["id"]):
                    cs = store.get_changeset(changeset_id)
                    add(
                        {
                            "type": "check_changeset",
                            "id": changeset_id,
                            "field": field,
                            "project": cs["project_id"],
                            "workspace": cs["workspace_id"],
                            "changeset": changeset_id,
                            "policy_object": changeset_id,
                            "policy_action": "changeset.read",
                            "created_at": cs["created_at"],
                        }
                    )

    for row in store.rows("SELECT * FROM exports WHERE artifact_id = ? OR exported_object_ids_json LIKE ? ORDER BY created_at, rowid", (artifact_id, f"%{artifact_id}%")):
        exported = loads(row["exported_object_ids_json"], [])
        if row["artifact_id"] != artifact_id and artifact_id not in exported:
            continue
        cs = store.get_changeset(row["changeset_id"])
        add(
            {
                "type": "export",
                "id": row["id"],
                "field": row["export_kind"],
                "project": cs["project_id"],
                "workspace": cs["workspace_id"],
                "changeset": cs["id"],
                "policy_object": cs["id"],
                "policy_action": "changeset.read",
                "created_at": row["created_at"],
            }
        )

    for row in store.rows("SELECT * FROM releases WHERE artifacts_json LIKE ? ORDER BY created_at, rowid", (f"%{artifact_id}%",)):
        if artifact_id not in loads(row["artifacts_json"], []):
            continue
        add(
            {
                "type": "release",
                "id": row["id"],
                "field": "artifacts",
                "project": row["project_id"],
                "workspace": None,
                "changeset": None,
                "policy_object": row["id"],
                "policy_action": "release.read",
                "created_at": row["created_at"],
            }
        )

    for row in store.rows("SELECT * FROM events WHERE object_refs_json LIKE ? ORDER BY created_at, rowid", (f"%{artifact_id}%",)):
        if str(row["kind"]).startswith("policy."):
            continue
        refs = loads(row["object_refs_json"], {})
        if not json_contains_string(refs, artifact_id):
            continue
        policy_object = refs.get("changeset") or refs.get("release") or row["workspace_id"]
        policy_action = None
        if policy_object:
            if str(policy_object).startswith("cs_"):
                policy_action = "changeset.read"
            elif str(policy_object).startswith("rel_"):
                policy_action = "release.read"
            elif str(policy_object).startswith("ws_"):
                policy_action = "workspace.read"
        add(
            {
                "type": "event",
                "id": row["id"],
                "field": row["kind"],
                "project": row["project_id"],
                "workspace": row["workspace_id"],
                "changeset": refs.get("changeset"),
                "policy_object": policy_object,
                "policy_action": policy_action,
                "created_at": row["created_at"],
            }
        )
    return origins


def artifact_event_context(store: Store, artifact_id: str) -> dict[str, str | None]:
    for origin in artifact_origins(store, artifact_id):
        project_id = origin.get("project")
        workspace_id = origin.get("workspace")
        task_id = None
        if workspace_id:
            try:
                task_id = store.get_workspace(str(workspace_id))["task_id"]
            except TriError:
                task_id = None
        return {"project": project_id, "workspace": workspace_id, "task": task_id}
    return {"project": None, "workspace": None, "task": None}


def artifact_origin_view(store: Store, origin: dict[str, Any], *, actor: str) -> dict[str, Any]:
    view = dict(origin)
    workspace_id = view.get("workspace")
    if isinstance(workspace_id, str):
        try:
            decision, _ = object_access_decision(store, workspace_id, actor=actor, action="workspace.read")
        except TriError:
            decision = {"allowed": False}
        if not decision["allowed"]:
            view["workspace"] = None
            view["workspace_redacted"] = True
    changeset_id = view.get("changeset")
    if isinstance(changeset_id, str):
        try:
            decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
        except TriError:
            decision = {"allowed": False}
        if not decision["allowed"]:
            view["changeset"] = None
            view["changeset_redacted"] = True
    return view


def artifact_access_decision(store: Store, artifact_id: str, *, actor: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    origins = artifact_origins(store, artifact_id)
    selectors = actor_selectors(actor)
    allowed = False
    reason = "missing_allow"
    grants: list[str] = []
    matched_origins: list[dict[str, Any]] = []
    visible_origins: list[dict[str, Any]] = []
    redacted_origins = 0
    if "role:maintainer" in selectors:
        allowed = True
        reason = "maintainer"
        grants.append("role:maintainer")
        visible_origins = origins
    for origin in origins:
        policy_object = origin.get("policy_object")
        policy_action = origin.get("policy_action")
        if not policy_object or not policy_action:
            if "role:maintainer" not in selectors:
                redacted_origins += 1
            continue
        if origin.get("type") == "release":
            try:
                release_view = release_data(store, store.get_release(str(origin["id"])), actor=actor)
            except TriError:
                release_view = {"artifacts": []}
            if artifact_id not in release_view.get("artifacts", []):
                if "role:maintainer" not in selectors:
                    redacted_origins += 1
                continue
        decision, _ = object_access_decision(store, policy_object, actor=actor, action=policy_action)
        if decision["allowed"]:
            allowed = True
            reason = "origin_access"
            grants.extend(decision["grants"])
            visible_origin = artifact_origin_view(store, origin, actor=actor)
            if visible_origin not in visible_origins:
                visible_origins.append(visible_origin)
            matched_origins.append({**visible_origin, "decision_reason": decision["reason"], "matched_grants": decision.get("matched_grants", [])})
        elif "role:maintainer" not in selectors:
            redacted_origins += 1
    if not origins and "role:maintainer" not in selectors:
        reason = "unowned_artifact"
    decision = {
        "id": new_id("decision"),
        "allowed": allowed,
        "actor": actor,
        "action": "artifact.read",
        "object": artifact_id,
        "object_type": "artifact",
        "policies": ["local_policy_v0"],
        "actor_selectors": selectors,
        "origins": visible_origins,
        "origin_count": len(origins),
        "redacted_origins": redacted_origins,
        "matched_origins": matched_origins,
        "grants": sorted(set(grants)),
        "denials": [] if allowed else ["policy_denied"],
        "redactions": [],
        "reason_code": "allowed" if allowed else "policy_denied",
        "reason": reason,
        "event": None,
    }
    diagnostics = [] if allowed else [
        decision_denial(
            "policy_denied",
            "Actor is not allowed to access this artifact.",
            object=artifact_id,
            action="artifact.read",
            origin_count=len(origins),
            redacted_origins=len(origins),
        )
    ]
    return decision, diagnostics


def require_artifact_access(store: Store, artifact_id: str, *, actor: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    decision, diagnostics = artifact_access_decision(store, artifact_id, actor=actor)
    if diagnostics:
        ctx = artifact_event_context(store, artifact_id)
        evt = store.record_policy_decision(
            actor=actor,
            action="artifact.read",
            object_id=artifact_id,
            decision=decision,
            project_id=ctx["project"],
            workspace_id=ctx["workspace"],
            task_id=ctx["task"],
            kind="policy.denied",
        )
        diagnostic = diagnostics[0]
        diagnostic["decision"] = decision["id"]
        diagnostic["event"] = evt
        diagnostic["explain_command"] = ["tri", "policy", "explain", decision["id"]]
        fields = {key: value for key, value in diagnostic.items() if key not in {"code", "severity", "message", "retryable"}}
        raise TriError(diagnostic["code"], diagnostic["message"], severity=diagnostic["severity"], retryable=diagnostic["retryable"], **fields)
    return decision, decision["origins"]


def artifact_view(store: Store, row: sqlite3.Row, *, actor: str, include_text: bool = False) -> dict[str, Any]:
    decision, origins = require_artifact_access(store, row["id"], actor=actor)
    data = artifact_data(row, include_text=include_text)
    data["origins"] = origins
    data["access"] = decision
    return data


def cmd_search(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if not args.query:
        raise TriError("invalid_command", "Search query cannot be empty.")
    project = store.get_project(args.project)
    scopes = resolve_search_scopes(args.scope)
    limit = max(1, min(args.limit, 1000))
    path_glob = args.glob or "*"
    matches: list[dict[str, Any]] = []
    counters = {
        "objects_searched": 0,
        "snapshots_searched": 0,
        "artifacts_searched": 0,
        "policy_denied_skipped": 0,
        "redacted_skipped": 0,
        "binary_skipped": 0,
    }

    def full() -> bool:
        return len(matches) >= limit

    if "targets" in scopes:
        for row in store.rows("SELECT * FROM targets WHERE project_id = ? ORDER BY name", (project["id"],)):
            counters["objects_searched"] += 1
            access = search_access_summary(None, object_id=row["name"], action="target.read", reason="project_target")
            if append_snapshot_matches(
                store,
                matches,
                counters,
                query=args.query,
                case_sensitive=args.case_sensitive,
                limit=limit,
                project_id=project["id"],
                snapshot=row["snapshot_id"],
                hidden_patterns=hidden_patterns_for_snapshot(store, row["snapshot_id"], actor=args.actor),
                path_glob=path_glob,
                scope="targets",
                object_type="target",
                object_id=row["name"],
                provenance={"target": row["name"], "target_id": row["id"]},
                access=access,
            ):
                break

    if "packages" in scopes and not full():
        for row in store.rows("SELECT * FROM package_boundaries WHERE project_id = ? ORDER BY name", (project["id"],)):
            decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="package.read")
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            package = package_boundary_data(row, access=decision)
            access = search_access_summary(decision, object_id=row["id"], action="package.read", reason="project_package_boundary")
            provenance = {"package": row["id"], "name": row["name"]}
            for field in ("name", "path_globs", "visibility", "owners"):
                if append_json_field_matches(
                    matches,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    scope="packages",
                    object_type="package",
                    object_id=row["id"],
                    project_id=project["id"],
                    field=field,
                    value=package[field],
                    provenance=provenance,
                    access=access,
                ):
                    break
            if full():
                break

    if "workspaces" in scopes and not full():
        for row in store.rows("SELECT * FROM workspaces WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],)):
            decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="workspace.read")
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            access = search_access_summary(decision, object_id=row["id"], action="workspace.read")
            hidden_patterns = effective_hidden_patterns(store, row["project_id"], loads(row["hidden_paths_json"], []), actor=args.actor)
            workspace_provenance = {"workspace": row["id"], "source": "workspace_head"}
            if append_snapshot_matches(
                store,
                matches,
                counters,
                query=args.query,
                case_sensitive=args.case_sensitive,
                limit=limit,
                project_id=project["id"],
                snapshot=row["head_snapshot_id"],
                hidden_patterns=hidden_patterns,
                path_glob=path_glob,
                scope="workspaces",
                object_type="workspace",
                object_id=row["id"],
                provenance=workspace_provenance,
                access=access,
            ):
                break
            if args.include_history:
                seen = {row["head_snapshot_id"]}
                for op in store.rows("SELECT id, output_snapshot_id FROM operations WHERE workspace_id = ? ORDER BY created_at, rowid", (row["id"],)):
                    if op["output_snapshot_id"] in seen:
                        continue
                    seen.add(op["output_snapshot_id"])
                    if append_snapshot_matches(
                        store,
                        matches,
                        counters,
                        query=args.query,
                        case_sensitive=args.case_sensitive,
                        limit=limit,
                        project_id=project["id"],
                        snapshot=op["output_snapshot_id"],
                        hidden_patterns=hidden_patterns,
                        path_glob=path_glob,
                        scope="workspaces",
                        object_type="workspace",
                        object_id=row["id"],
                        provenance={"workspace": row["id"], "operation": op["id"], "source": "operation_snapshot"},
                        access=access,
                    ):
                        break
            if full():
                break

    if "changesets" in scopes and not full():
        for row in store.rows("SELECT * FROM changesets WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],)):
            decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="changeset.read")
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            access = search_access_summary(decision, object_id=row["id"], action="changeset.read")
            cs_data = changeset_data(store, row["id"], actor=args.actor)
            provenance = {"changeset": row["id"]}
            if cs_data["workspace"]:
                provenance["workspace"] = cs_data["workspace"]
            elif cs_data.get("redacted_workspace"):
                provenance["workspace_redacted"] = True
            for field in ("title", "status", "touched_paths"):
                if append_json_field_matches(
                    matches,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    scope="changesets",
                    object_type="changeset",
                    object_id=row["id"],
                    project_id=project["id"],
                    field=field,
                    value=cs_data[field],
                    provenance=provenance,
                    access=access,
                ):
                    break
            if full() or append_snapshot_matches(
                store,
                matches,
                counters,
                query=args.query,
                case_sensitive=args.case_sensitive,
                limit=limit,
                project_id=project["id"],
                snapshot=row["head_snapshot_id"],
                hidden_patterns=effective_hidden_patterns(store, row["project_id"], loads(row["hidden_paths_json"], []), actor=args.actor),
                path_glob=path_glob,
                scope="changesets",
                object_type="changeset",
                object_id=row["id"],
                provenance={**provenance, "source": "changeset_head"},
                access=access,
            ):
                break

    if "tasks" in scopes and not full():
        for row in store.rows("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],)):
            decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="task.read")
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            access = search_access_summary(decision, object_id=row["id"], action="task.read")
            task = task_data(store, row["id"], actor=args.actor)
            provenance = {"task": row["id"]}
            for field in ("title", "body", "acceptance_criteria", "required_checks", "status"):
                if append_json_field_matches(
                    matches,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    scope="tasks",
                    object_type="task",
                    object_id=row["id"],
                    project_id=project["id"],
                    field=field,
                    value=task[field],
                    provenance=provenance,
                    access=access,
                ):
                    break
            if full():
                break

    if "reviews" in scopes and not full():
        for row in store.rows(
            """
            SELECT reviews.* FROM reviews
            JOIN changesets ON reviews.changeset_id = changesets.id
            WHERE changesets.project_id = ?
            ORDER BY reviews.created_at, reviews.rowid
            """,
            (project["id"],),
        ):
            decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="review.read")
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            access = search_access_summary(decision, object_id=row["id"], action="review.read")
            changeset_decision, _ = object_access_decision(store, row["changeset_id"], actor=args.actor, action="changeset.read")
            can_read_changeset = changeset_decision["allowed"]
            provenance = {"review": row["id"]}
            if can_read_changeset:
                provenance["changeset"] = row["changeset_id"]
                check_summary = loads(row["check_summary_json"], {})
            else:
                provenance["changeset_redacted"] = True
                check_summary = {"redacted": True}
                counters["redacted_skipped"] += 1
            for field, value in (
                ("status", row["status"]),
                ("risk_summary", row["risk_summary"]),
                ("reviewers", loads(row["reviewers_json"], [])),
                ("check_summary", check_summary),
            ):
                if append_json_field_matches(
                    matches,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    scope="reviews",
                    object_type="review",
                    object_id=row["id"],
                    project_id=project["id"],
                    field=field,
                    value=value,
                    provenance=provenance,
                    access=access,
                ):
                    break
            if full():
                break
            for action in store.rows("SELECT * FROM review_actions WHERE review_id = ? ORDER BY created_at, rowid", (row["id"],)):
                action_data = review_action_data(action)
                action_fields = ("kind", "body", "metadata") if can_read_changeset else ("kind", "body")
                if not can_read_changeset and action_data.get("metadata"):
                    counters["redacted_skipped"] += 1
                for field in action_fields:
                    if append_json_field_matches(
                        matches,
                        query=args.query,
                        case_sensitive=args.case_sensitive,
                        limit=limit,
                        scope="reviews",
                        object_type="review_action",
                        object_id=action["id"],
                        project_id=project["id"],
                        field=field,
                        value=action_data[field],
                        provenance={**provenance, "action": action["id"]},
                        access=access,
                    ):
                        break
                if full():
                    break
            if full():
                break

    if "checks" in scopes and not full():
        for row in store.rows(
            """
            SELECT DISTINCT check_runs.* FROM check_runs
            LEFT JOIN workspaces ON check_runs.workspace_id = workspaces.id
            LEFT JOIN changesets ON check_runs.changeset_id = changesets.id
            WHERE workspaces.project_id = ? OR changesets.project_id = ?
            ORDER BY check_runs.created_at, check_runs.rowid
            """,
            (project["id"], project["id"]),
        ):
            raw_check = check_data(store, row["id"])
            decision = check_access_decision(store, raw_check, actor=args.actor)
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            access = search_access_summary(decision, object_id=raw_check["id"], action="check.read")
            check = check_view(store, row["id"], actor=args.actor)
            provenance = {"check": check["id"]}
            if check["workspace"]:
                provenance["workspace"] = check["workspace"]
            elif check.get("redacted_workspace"):
                provenance["workspace_redacted"] = True
            if check["changeset"]:
                provenance["changeset"] = check["changeset"]
            elif check.get("redacted_changeset"):
                provenance["changeset_redacted"] = True
            if check.get("linked_changesets"):
                provenance["linked_changesets"] = check["linked_changesets"]
            for field in ("command", "status", "reproducibility", "exit_status"):
                if append_json_field_matches(
                    matches,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    scope="checks",
                    object_type="check",
                    object_id=check["id"],
                    project_id=project["id"],
                    field=field,
                    value=check[field],
                    provenance=provenance,
                    access=access,
                ):
                    break
            if full():
                break
            for log_name, artifact_id in check["logs"].items():
                if append_artifact_matches(
                    store,
                    matches,
                    counters,
                    artifact_id=artifact_id,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    project_id=project["id"],
                    scope="checks",
                    object_type="check",
                    object_id=check["id"],
                    field=f"log.{log_name}",
                    provenance=provenance,
                    actor=args.actor,
                ):
                    break
            if full():
                break

    if "releases" in scopes and not full():
        for row in store.rows("SELECT * FROM releases WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],)):
            decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="release.read")
            if not decision["allowed"]:
                counters["policy_denied_skipped"] += 1
                continue
            counters["objects_searched"] += 1
            access = search_access_summary(decision, object_id=row["id"], action="release.read")
            release = release_data(store, row, actor=args.actor)
            provenance = {"release": row["id"]}
            for field in ("status", "included_changesets", "packages", "checks", "advisories_disclosed", "rollback_target"):
                if append_json_field_matches(
                    matches,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    scope="releases",
                    object_type="release",
                    object_id=row["id"],
                    project_id=project["id"],
                    field=field,
                    value=release[field],
                    provenance=provenance,
                    access=access,
                ):
                    break
            if full():
                break
            for artifact_id in release["artifacts"]:
                if append_artifact_matches(
                    store,
                    matches,
                    counters,
                    artifact_id=artifact_id,
                    query=args.query,
                    case_sensitive=args.case_sensitive,
                    limit=limit,
                    project_id=project["id"],
                    scope="releases",
                    object_type="release",
                    object_id=row["id"],
                    field="artifact",
                    provenance=provenance,
                    actor=args.actor,
                ):
                    break
            if full():
                break

    data = {
        "project": project["id"],
        "query": args.query,
        "scopes": scopes,
        "glob": path_glob,
        "include_history": bool(args.include_history),
        "matches": matches,
        "policy_denied_skipped": counters["policy_denied_skipped"],
        "redacted_skipped": counters["redacted_skipped"],
        "binary_skipped": counters["binary_skipped"],
        "objects_searched": counters["objects_searched"],
        "snapshots_searched": counters["snapshots_searched"],
        "artifacts_searched": counters["artifacts_searched"],
        "truncated": full(),
    }
    return envelope("tri.search", command, actor=args.actor, start=start, data=data)


def cmd_files_delete(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    path = normalize_path(args.path)

    def apply(entries: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        if path not in entries:
            raise TriError("path_not_found", "Path not found.", object=path)
        entries.pop(path)
        return dict(sorted(entries.items())), [path]

    result, events = mutate_workspace(
        store,
        args.workspace,
        actor=args.actor,
        kind="delete_file",
        apply_change=apply,
        metadata={"path": path},
        expect_head=args.expect_head,
    )
    return envelope("tri.files.delete", command, actor=args.actor, start=start, events=events, data=result)


def cmd_files_move(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    src = normalize_path(args.src)
    dest = normalize_path(args.dest)

    def apply(entries: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        if src not in entries:
            raise TriError("path_not_found", "Path not found.", object=src)
        entries[dest] = entries.pop(src)
        return dict(sorted(entries.items())), [src, dest]

    result, events = mutate_workspace(
        store,
        args.workspace,
        actor=args.actor,
        kind="move_file",
        apply_change=apply,
        metadata={"src": src, "dest": dest},
        expect_head=args.expect_head,
    )
    return envelope("tri.files.move", command, actor=args.actor, start=start, events=events, data=result)


def cmd_diff(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    hidden_patterns: list[str] = []
    if args.head_ref:
        base, base_hidden_patterns = resolve_ref_context(store, args.ref, actor=args.actor, action="workspace.read")
        head, head_hidden_patterns = resolve_ref_context(store, args.head_ref, actor=args.actor, action="workspace.read")
        hidden_patterns = sorted(set(base_hidden_patterns) | set(head_hidden_patterns))
    elif args.ref.startswith("ws_"):
        ws = store.get_workspace(args.ref)
        require_object_access(store, ws["id"], actor=args.actor, action="workspace.read")
        base, head = ws["base_snapshot_id"], ws["head_snapshot_id"]
        hidden_patterns = effective_hidden_patterns(store, ws["project_id"], loads(ws["hidden_paths_json"], []), actor=args.actor)
    elif args.ref.startswith("cs_"):
        cs = store.get_changeset(args.ref)
        require_object_access(store, cs["id"], actor=args.actor, action="changeset.read")
        base, head = cs["base_snapshot_id"], cs["head_snapshot_id"]
        hidden_patterns = effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=args.actor)
    else:
        raise TriError("path_not_found", "diff expects a workspace or changeset id.", object=args.ref)
    return envelope("tri.diff", command, actor=args.actor, start=start, data=diff_data(store, base, head, hidden_patterns, path_patterns=args.path, mode=args.mode))


def run_check_for_workspace(
    store: Store,
    ws: sqlite3.Row,
    cmd: list[str],
    *,
    actor: str,
    command: list[str],
    start: float,
    rerun_of: str | None = None,
    secret_lease_ids: list[str] | None = None,
    assumptions: list[str] | None = None,
    import_changes: bool = True,
    use_env: bool = True,
    sparse_paths: list[str] | None = None,
    sandbox_mode: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    check_id = new_id("check")
    run_lease = RunLease(store, kind="run", key=check_id, workspace_id=ws["id"], before_snapshot=ws["head_snapshot_id"])
    failed = True
    try:
        result = _run_check_for_workspace(
            store,
            ws,
            cmd,
            check_id=check_id,
            run_lease=run_lease,
            actor=actor,
            command=command,
            start=start,
            rerun_of=rerun_of,
            secret_lease_ids=secret_lease_ids,
            assumptions=assumptions,
            import_changes=import_changes,
            use_env=use_env,
            sparse_paths=sparse_paths,
            sandbox_mode=sandbox_mode,
            timeout=timeout,
        )
        failed = False
        return result
    finally:
        run_lease.finish(failed)


def _run_check_for_workspace(
    store: Store,
    ws: sqlite3.Row,
    cmd: list[str],
    *,
    check_id: str,
    run_lease: RunLease,
    actor: str,
    command: list[str],
    start: float,
    rerun_of: str | None = None,
    secret_lease_ids: list[str] | None = None,
    assumptions: list[str] | None = None,
    import_changes: bool = True,
    use_env: bool = True,
    sparse_paths: list[str] | None = None,
    sandbox_mode: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can run checks.", object=ws["id"])
    policy_decision = require_object_access(store, ws["id"], actor=actor, action="workspace.write")
    if not cmd:
        raise TriError("path_not_found", "No command supplied for tri run.")
    before = ws["head_snapshot_id"]
    before_entries = store.load_snapshot_tree(before)
    sparse = normalize_path_patterns(sparse_paths)
    run_entries = sparse_entries(before_entries, sparse)
    env_plan = plan_environments(store, ws["project_id"], before_entries, actor=actor, workspace=ws, purpose="run") if use_env else EnvPlan()
    secret_leases, secret_env, redactions = active_secret_leases(store, ws["id"], secret_lease_ids or [], actor=actor)
    generated_configs = generated_config_records_for_run(store, ws["id"], actor=actor)
    task_run = latest_workspace_task_run(store, ws["id"])
    normalized_assumptions = normalize_assumptions(assumptions)
    work = store.runs / check_id / "work"
    box = command_sandbox(store, run_lease.dir, purpose="run", requested=sandbox_mode, timeout=timeout)
    extra_env = dict(secret_env)
    if generated_configs:
        extra_env["TRI_CONFIG_DIR"] = str(work / ".tri" / "generated-config")
    env = sandbox_env(box, run_lease.dir, os.environ, extra_env)
    with store.conn:
        phase_evt = set_workspace_task_run_phase(store, ws, actor=actor, status="checking", reason="run")
        policy_event = new_id("evt")
        policy_decision["event"] = policy_event
        store.create_event(
            kind="policy.evaluated",
            actor=actor,
            event_id=policy_event,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"decision": policy_decision["id"], "action": "workspace.write", "object": ws["id"]},
            policy_result=policy_decision,
        )
        started_evt = store.create_event(
            kind="check.rerun_started" if rerun_of else "check.started",
            actor=actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={
                "check": check_id,
                "runner": "local",
                "command": cmd,
                "rerun_of": rerun_of,
                "secret_leases": [lease["id"] for lease in secret_leases],
                "generated_configs": generated_configs,
                "assumptions": normalized_assumptions,
            },
            input_fingerprints={"snapshot": before},
            policy_result=policy_decision,
        )
    events: list[str] = [policy_event, started_evt] if phase_evt is None else [phase_evt, policy_event, started_evt]
    materialization = materialize_for_run(
        store,
        run_entries,
        run_lease,
        # A partial checkout writes only its files; cloning the full cached base would defeat it.
        base_snapshot=None if sparse else ws["base_snapshot_id"],
        attachments=env_plan.attachments,
        allow_overlay=not env_plan.private and not sparse and box is None,
    )
    env_plan.finalize(materialization)
    run_lease.record_envs(env_plan)
    run_lease.record_sparse(sparse)
    run_lease.record_materialization(materialization)
    env_plan.install_private(work, env, box)
    materialize_generated_configs_for_run(store, materialization.writable_root, generated_configs)
    started = time.time()
    run_cwd = run_lease.dir if isinstance(materialization, OverlayMaterialization) else work
    proc = sandbox.run(box, materialization.wrap(cmd), cwd=run_cwd, env=env)
    run_lease.command_finished = import_changes
    duration_ms = int((time.time() - started) * 1000)
    if import_changes:
        rules = import_rules(store, ws["project_id"], before_entries)
        scanned_changed, scanned_deleted = materialization.scan(run_entries, ignore=rules)
        scanned_entries = entries_after_scan(store, materialization.content_root, before_entries, scanned_changed, scanned_deleted)
        after_entries = filter_generated_config_entries(before_entries, scanned_entries, generated_configs)
        paths = changed_paths(before_entries, after_entries)
    else:
        after_entries = before_entries
        paths = []
    file_change_ops: list[str] = []
    after = before
    stale_diagnostic: dict[str, Any] | None = None
    if paths:
        try:
            with store.conn:
                snapshot = store.create_snapshot(ws["project_id"], after_entries, parents=[before], source="workspace", actor=actor, metadata={"workspace": ws["id"], "operation": "run_file_changes"})
                op_id = new_id("op")
                store.conn.execute(
                    "INSERT INTO operations (id, workspace_id, actor, kind, input_snapshot_id, output_snapshot_id, path_set_json, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (op_id, ws["id"], actor, "run_file_changes", before, snapshot["id"], dumps(paths), dumps({"command": cmd, "rerun_of": rerun_of, "policy": policy_provenance_data(policy_decision)}), utcnow()),
                )
                updated = store.conn.execute("UPDATE workspaces SET head_snapshot_id = ?, updated_at = ? WHERE id = ? AND head_snapshot_id = ?", (snapshot["id"], utcnow(), ws["id"], before))
                if updated.rowcount != 1:
                    current = store.row("SELECT head_snapshot_id FROM workspaces WHERE id = ?", (ws["id"],))
                    raise TriError(
                        "stale_workspace_head",
                        "Workspace head changed while the command was running; command file changes were not imported.",
                        object=ws["id"],
                        expected=before,
                        actual=current["head_snapshot_id"] if current else None,
                        paths=paths,
                        retryable=True,
                    )
                evt = store.create_event(
                    kind="workspace.run_file_changes",
                    actor=actor,
                    project_id=ws["project_id"],
                    workspace_id=ws["id"],
                    task_id=ws["task_id"],
                    object_refs={"operation": op_id, "snapshot": snapshot["id"], "paths": paths},
                    input_fingerprints={"snapshot": before},
                    output_fingerprints={"snapshot": snapshot["id"]},
                    policy_result=policy_decision,
                )
                after = snapshot["id"]
                file_change_ops.append(op_id)
                events.append(evt)
        except TriError as exc:
            if exc.diagnostic["code"] != "stale_workspace_head":
                raise
            stale_diagnostic = exc.diagnostic
    stdout_bytes = redact_secret_bytes(proc.stdout, redactions)
    stderr_bytes = redact_secret_bytes(proc.stderr, redactions)
    cost = local_check_cost(cmd, duration_ms, stdout_bytes, stderr_bytes)
    stdout_artifact = write_artifact(store, "stdout", f"{check_id}-stdout.txt", stdout_bytes, labels=[])
    stderr_artifact = write_artifact(store, "stderr", f"{check_id}-stderr.txt", stderr_bytes, labels=[])
    status = "stale" if stale_diagnostic else ("passed" if proc.returncode == 0 else "failed")
    materialized_directory = materialized_directory_record(
        path=work,
        input_snapshot=before,
        output_snapshot=after,
        before_entries=before_entries,
        after_entries=after_entries,
        mode="ephemeral_workspace_check",
        materialization=materialization,
    )
    materialized_directory["environments"] = env_plan.records
    if sparse:
        materialized_directory["sparse_paths"] = sparse
        materialized_directory["materialized_files"] = len(run_entries)
        materialized_directory["total_files"] = len(before_entries)
    environment_policy = check_environment_policy(secret_leases, secret_env)
    environment_policy["generated_configs"] = generated_configs
    environment_policy["generated_config_paths"] = [config["path"] for config in generated_configs]
    environment_policy["sandbox"] = {**box.describe(), "timed_out": proc.timed_out} if box else {"kind": "off", "isolated": False}
    reproducibility = "not_imported_stale_workspace_head" if stale_diagnostic else "local_replayable"
    completed_refs = {
        "check": check_id,
        "status": status,
        "rerun_of": rerun_of,
        "secret_leases": [lease["id"] for lease in secret_leases],
        "generated_configs": generated_configs,
        "assumptions": normalized_assumptions,
    }
    if stale_diagnostic:
        completed_refs["diagnostic"] = stale_diagnostic["code"]
        completed_refs["unimported_paths"] = paths
    with store.conn:
        store.conn.execute(
            """
            INSERT INTO check_runs (
              id, workspace_id, changeset_id, task_run_id, rerun_of, actor, status, runner, command_json,
              snapshot_before, snapshot_after, exit_status, duration_ms, logs_json,
              artifacts_json, secret_leases_json, materialized_directory_json, environment_policy_json,
              file_changes_json, cost_json, assumptions_json, reproducibility, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check_id,
                ws["id"],
                None,
                task_run["id"] if task_run else None,
                rerun_of,
                actor,
                status,
                "local",
                dumps(cmd),
                before,
                after,
                proc.returncode,
                duration_ms,
                dumps({"stdout": stdout_artifact, "stderr": stderr_artifact}),
                dumps([stdout_artifact, stderr_artifact]),
                dumps([lease["id"] for lease in secret_leases]),
                dumps(materialized_directory),
                dumps(environment_policy),
                dumps(file_change_ops),
                dumps(cost),
                dumps(normalized_assumptions),
                reproducibility,
                utcnow(),
            ),
        )
        attach_assumptions_to_task_run(store, task_run["id"] if task_run else None, normalized_assumptions)
        evt = store.create_event(
            kind="check.rerun_completed" if rerun_of else "check.completed",
            actor=actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs=completed_refs,
            input_fingerprints={"snapshot": before},
            output_fingerprints={"snapshot": after},
            policy_result=policy_decision,
        )
        events.append(evt)
    return envelope("tri.check.inspect", command, actor=actor, start=start, ok=stale_diagnostic is None, events=events, diagnostics=[stale_diagnostic] if stale_diagnostic else [], data=check_view(store, check_id, actor=actor))


def cmd_run(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    cmd = list(args.command)
    lease_ids = list(args.with_lease or [])
    assumptions = list(args.assumption or [])
    import_changes = not args.no_import_changes
    use_env = not args.no_env
    sparse_paths = list(args.paths or [])
    sandbox_mode = "required" if args.sandbox else None
    timeout = args.timeout
    while cmd and cmd[0] in ("--with-lease", "--assumption", "--no-import-changes", "--no-env", "--paths", "--sandbox", "--timeout"):
        if cmd[0] == "--sandbox":
            sandbox_mode = "required"
            cmd = cmd[1:]
            continue
        if cmd[0] == "--no-import-changes":
            import_changes = False
            cmd = cmd[1:]
            continue
        if cmd[0] == "--no-env":
            use_env = False
            cmd = cmd[1:]
            continue
        if len(cmd) < 2:
            raise TriError("invalid_command", f"{cmd[0]} requires a value.")
        if cmd[0] == "--with-lease":
            lease_ids.append(cmd[1])
        elif cmd[0] == "--paths":
            sparse_paths.append(cmd[1])
        elif cmd[0] == "--timeout":
            try:
                timeout = float(cmd[1])
            except ValueError:
                raise TriError("invalid_command", "--timeout takes seconds.", value=cmd[1]) from None
        else:
            assumptions.append(cmd[1])
        cmd = cmd[2:]
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return run_check_for_workspace(
        store,
        ws,
        cmd,
        actor=args.actor,
        command=command,
        start=start,
        secret_lease_ids=lease_ids,
        assumptions=assumptions,
        import_changes=import_changes,
        use_env=use_env,
        sparse_paths=sparse_paths,
        sandbox_mode=sandbox_mode,
        timeout=timeout,
    )


def write_artifact(store: Store, kind: str, name: str, data: bytes, *, labels: list[str]) -> str:
    artifact_id = new_id("artifact")
    path = store.artifacts / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    store.conn.execute(
        "INSERT INTO artifacts (id, kind, path, size, labels_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (artifact_id, kind, str(path), len(data), dumps(labels), utcnow()),
    )
    return artifact_id


def check_data(store: Store, check_id: str) -> dict[str, Any]:
    row = store.row("SELECT * FROM check_runs WHERE id = ?", (check_id,))
    if row is None:
        raise TriError("path_not_found", "Check run not found.", object=check_id)
    materialized_directory = loads(row["materialized_directory_json"], {})
    if materialized_directory.get("path"):
        materialized_directory["path"] = None
        materialized_directory["path_redacted"] = True
    command = loads(row["command_json"], [])
    logs = loads(row["logs_json"], {})
    artifacts = loads(row["artifacts_json"], [])
    secret_leases = loads(row["secret_leases_json"], [])
    environment_policy = loads(row["environment_policy_json"], {})
    file_changes = loads(row["file_changes_json"], [])
    assumptions = loads(row["assumptions_json"], [])
    inputs = {
        "workspace": row["workspace_id"],
        "changeset": row["changeset_id"],
        "task_run": row["task_run_id"],
        "snapshot": row["snapshot_before"],
        "command": command,
        "secret_leases": secret_leases,
        "assumptions": assumptions,
        "environment_policy": environment_policy,
        "materialized_directory": {
            "mode": materialized_directory.get("mode"),
            "snapshot": materialized_directory.get("input_snapshot"),
            "fingerprint": materialized_directory.get("fingerprint_before"),
        },
    }
    outputs = {
        "snapshot": row["snapshot_after"],
        "status": row["status"],
        "exit_status": row["exit_status"],
        "logs": logs,
        "artifacts": artifacts,
        "file_changes": file_changes,
        "materialized_directory": {
            "mode": materialized_directory.get("mode"),
            "snapshot": materialized_directory.get("output_snapshot"),
            "fingerprint": materialized_directory.get("fingerprint_after"),
            "mutated": materialized_directory.get("mutated", False),
        },
    }
    return {
        "id": row["id"],
        "workspace": row["workspace_id"],
        "changeset": row["changeset_id"],
        "task_run": row["task_run_id"],
        "rerun_of": row["rerun_of"],
        "actor": row["actor"],
        "status": row["status"],
        "runner": row["runner"],
        "command": command,
        "snapshot_before": row["snapshot_before"],
        "snapshot_after": row["snapshot_after"],
        "exit_status": row["exit_status"],
        "duration_ms": row["duration_ms"],
        "logs": logs,
        "artifacts": artifacts,
        "secret_leases": secret_leases,
        "materialized_directory": materialized_directory,
        "environment_policy": environment_policy,
        "inputs": inputs,
        "outputs": outputs,
        "file_changes": file_changes,
        "cost": loads(row["cost_json"], {}),
        "assumptions": assumptions,
        "reproducibility": row["reproducibility"],
        "created_at": row["created_at"],
    }


def check_list_context(store: Store, ref: str, *, actor: str) -> dict[str, Any]:
    if ref.startswith("ws_"):
        workspace = store.get_workspace(ref)
        require_object_access(store, workspace["id"], actor=actor, action="workspace.read")
        rows = store.rows("SELECT id FROM check_runs WHERE workspace_id = ? ORDER BY created_at, rowid", (workspace["id"],))
        return {
            "ref": ref,
            "object_type": "workspace",
            "workspace": workspace["id"],
            "redacted_workspace": False,
            "changeset": None,
            "check_ids": [row["id"] for row in rows],
        }
    if ref.startswith("cs_"):
        changeset = store.get_changeset(ref)
        require_object_access(store, changeset["id"], actor=actor, action="changeset.read")
        workspace_id, redacted_workspace = workspace_ref_for_actor(store, changeset["workspace_id"], actor=actor)
        return {
            "ref": ref,
            "object_type": "changeset",
            "workspace": workspace_id,
            "redacted_workspace": redacted_workspace,
            "changeset": changeset["id"],
            "check_ids": loads(changeset["checks_json"], []),
        }
    raise TriError("path_not_found", "Expected a workspace or changeset id.", object=ref)


def cmd_checks_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    context = check_list_context(store, args.ref, actor=args.actor)
    checks = []
    redacted = 0
    for check_id in context.pop("check_ids"):
        try:
            checks.append(check_view(store, check_id, actor=args.actor))
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return envelope("tri.check.list", command, actor=args.actor, start=start, data={**context, "checks": checks, "redacted": redacted})


def cmd_checks_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.check.inspect", command, actor=args.actor, start=start, data=check_view(store, args.check, actor=args.actor))


def cmd_checks_artifacts(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    check = check_data(store, args.check)
    require_check_access(store, check, actor=args.actor)
    artifacts = [artifact_data(store.get_artifact(artifact_id)) for artifact_id in check["artifacts"]]
    return envelope("tri.check.artifacts", command, actor=args.actor, start=start, data={"check": args.check, "artifacts": artifacts})


def cmd_checks_stream(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.offset < 0:
        raise TriError("invalid_command", "Log stream offset must be non-negative.", object=args.check)
    if args.limit <= 0:
        raise TriError("invalid_command", "Log stream limit must be positive.", object=args.check)
    check = check_data(store, args.check)
    require_check_access(store, check, actor=args.actor)
    logs = check["logs"]
    artifact_id = logs.get(args.log)
    if not artifact_id:
        raise TriError("path_not_found", "Check log not found.", object=args.check, log=args.log)
    artifact = store.get_artifact(artifact_id)
    raw = Path(artifact["path"]).read_bytes()
    chunk = raw[args.offset : args.offset + args.limit]
    next_offset = min(args.offset + len(chunk), len(raw))
    try:
        text = chunk.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = base64.b64encode(chunk).decode()
        encoding = "base64"
    return envelope(
        "tri.check.stream",
        command,
        actor=args.actor,
        start=start,
        data={
            "check": args.check,
            "log": args.log,
            "artifact": artifact_id,
            "offset": args.offset,
            "next_offset": next_offset,
            "limit": args.limit,
            "total_bytes": len(raw),
            "eof": next_offset >= len(raw),
            "encoding": encoding,
            "text": text,
        },
    )


def cmd_checks_rerun(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    original = check_data(store, args.check)
    if not original["workspace"]:
        raise TriError("path_not_found", "Check run is not attached to a workspace.", object=args.check)
    ws = store.get_workspace(original["workspace"])
    return run_check_for_workspace(store, ws, original["command"], actor=args.actor, command=command, start=start, rerun_of=args.check, secret_lease_ids=original["secret_leases"], assumptions=original["assumptions"])


def cmd_checks_compare(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    checks = []
    for check_id in args.checks:
        check = check_view(store, check_id, actor=args.actor)
        checks.append(
            {
                "id": check["id"],
                "workspace": check["workspace"],
                "status": check["status"],
                "exit_status": check["exit_status"],
                "command": check["command"],
                "snapshot_before": check["snapshot_before"],
                "snapshot_after": check["snapshot_after"],
                "duration_ms": check["duration_ms"],
                "artifact_count": len(check["artifacts"]),
                "file_change_count": len(check["file_changes"]),
                "rerun_of": check["rerun_of"],
            }
        )
    baseline = checks[0] if checks else None
    return envelope(
        "tri.check.compare",
        command,
        actor=args.actor,
        start=start,
        data={
            "checks": checks,
            "same_command": len({dumps(check["command"]) for check in checks}) <= 1,
            "all_passed": all(check["status"] == "passed" for check in checks),
            "status_changed_from_first": [check["id"] for check in checks[1:] if baseline and check["status"] != baseline["status"]],
        },
    )


DEFAULT_MERGE_RULE_ALLOW = ["role:agent", "role:human", "role:service", "role:maintainer"]


def merge_rule_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project": row["project_id"],
        "name": row["name"],
        "path_globs": loads(row["path_globs_json"], []),
        "exclude_globs": loads(row["exclude_globs_json"], []),
        "allow": loads(row["allow_json"], []),
        "max_files": row["max_files"],
        "verify_after": row_value(row, "verify_check_set_id"),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "removed_at": row["removed_at"],
    }


def resolve_merge_rule(store: Store, project_id: str, name_or_id: str) -> sqlite3.Row:
    row = store.row(
        "SELECT * FROM merge_rules WHERE project_id = ? AND (id = ? OR name = ?) ORDER BY status = 'active' DESC, created_at DESC",
        (project_id, name_or_id, name_or_id),
    )
    if row is None:
        raise TriError("path_not_found", "Merge rule not found.", object=name_or_id)
    return row


def merge_rule_matches(rule: dict[str, Any], *, selectors: set[str], changed: list[str]) -> bool:
    if not changed or not selectors & set(rule["allow"]):
        return False
    if rule["max_files"] is not None and len(changed) > rule["max_files"]:
        return False
    for path in changed:
        if is_secret_like_path(path):
            return False
        if not path_matches_hidden(path, rule["path_globs"]) or path_matches_hidden(path, rule["exclude_globs"]):
            return False
    return True


def matching_merge_rule(store: Store, cs: sqlite3.Row, *, actor: str, changed: list[str]) -> dict[str, Any] | None:
    """A merge rule replaces human review for small changes in declared paths.

    Every other merge gate (checks, conflicts, secrets, hidden paths, stacks)
    still applies; advisory-linked changesets never merge through a rule.
    """
    if cs["advisory_id"] or not changed:
        return None
    selectors = set(actor_selectors(actor))
    for row in store.rows("SELECT * FROM merge_rules WHERE project_id = ? AND status = 'active' ORDER BY created_at, rowid", (cs["project_id"],)):
        rule = merge_rule_data(row)
        if merge_rule_matches(rule, selectors=selectors, changed=changed):
            return rule
    return None


def require_maintainer(actor: str, *, action: str, object_id: str, message: str) -> None:
    if "role:maintainer" not in actor_selectors(actor):
        raise TriError("policy_denied", message, object=object_id, action=action, required_grant="role:maintainer")


def cmd_merge_rules_add(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_maintainer(args.actor, action="merge_rule.create", object_id=args.project, message="Only maintainers can create merge rules.")
    project = store.get_project(args.project)
    globs = [normalize_path(glob) for glob in args.path or []]
    if not globs:
        raise TriError("invalid_command", "A merge rule requires at least one --path glob.")
    if args.max_files is not None and args.max_files < 1:
        raise TriError("invalid_command", "--max-files must be at least 1.")
    allow = sorted(set(args.allow or DEFAULT_MERGE_RULE_ALLOW))
    verify_check_set = resolve_check_set(store, project["id"], args.verify_after)["id"] if args.verify_after else None
    if store.row("SELECT id FROM merge_rules WHERE project_id = ? AND name = ? AND status = 'active'", (project["id"], args.name)):
        raise TriError("invalid_command", "An active merge rule with this name already exists.", object=args.name)
    rule_id = new_id("mrule")
    with store.conn:
        store.conn.execute(
            "INSERT INTO merge_rules (id, project_id, name, path_globs_json, exclude_globs_json, allow_json, max_files, verify_check_set_id, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (rule_id, project["id"], args.name, dumps(globs), dumps([normalize_path(glob) for glob in args.exclude or []]), dumps(allow), args.max_files, verify_check_set, args.actor, utcnow()),
        )
        evt = store.create_event(kind="merge_rule.created", actor=args.actor, project_id=project["id"], object_refs={"merge_rule": rule_id, "name": args.name})
    return envelope("tri.merge_rule.inspect", command, actor=args.actor, start=start, events=[evt], data=merge_rule_data(resolve_merge_rule(store, project["id"], rule_id)))


def cmd_merge_rules_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    rows = store.rows("SELECT * FROM merge_rules WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],))
    rules = [merge_rule_data(row) for row in rows if args.all or row["status"] == "active"]
    return envelope("tri.merge_rule.list", command, actor=args.actor, start=start, data={"project": project["id"], "merge_rules": rules})


def cmd_merge_rules_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    return envelope("tri.merge_rule.inspect", command, actor=args.actor, start=start, data=merge_rule_data(resolve_merge_rule(store, project["id"], args.name)))


def cmd_merge_rules_remove(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_maintainer(args.actor, action="merge_rule.remove", object_id=args.project, message="Only maintainers can remove merge rules.")
    project = store.get_project(args.project)
    row = resolve_merge_rule(store, project["id"], args.name)
    with store.conn:
        store.conn.execute("UPDATE merge_rules SET status = 'removed', removed_at = ? WHERE id = ?", (utcnow(), row["id"]))
        evt = store.create_event(kind="merge_rule.removed", actor=args.actor, project_id=project["id"], object_refs={"merge_rule": row["id"], "name": row["name"]})
    return envelope("tri.merge_rule.inspect", command, actor=args.actor, start=start, events=[evt], data=merge_rule_data(resolve_merge_rule(store, project["id"], row["id"])))


# --- Post-merge verification ---------------------------------------------------
#
# A merge rule with --verify-after lets a changeset merge before its checks
# pass. The merge queues a verification; `tri verify run` runs the rule's check
# set on the merged snapshot and, on failure, reverts the changeset's effect on
# the current target with a three-way merge so later merges survive.

VERIFY_ROLES = ("role:maintainer", "role:service")


def verification_data(store: Store, row: sqlite3.Row) -> dict[str, Any]:
    target = store.row("SELECT name FROM targets WHERE id = ?", (row["target_id"],))
    return {
        "id": row["id"],
        "project": row["project_id"],
        "changeset": row["changeset_id"],
        "target": target["name"] if target else None,
        "merged_snapshot": row["merged_snapshot_id"],
        "pre_merge_snapshot": row["pre_merge_snapshot_id"],
        "check_set": row["check_set_id"],
        "merge_rule": row["merge_rule_id"],
        "status": row["status"],
        "checks": loads(row["checks_json"], []),
        "revert_snapshot": row["revert_snapshot_id"],
        "result": loads(row["result_json"], {}),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def resolve_verification(store: Store, verification_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM verifications WHERE id = ?", (verification_id,))
    if row is None:
        raise TriError("path_not_found", "Verification not found.", object=verification_id)
    return row


def queue_post_merge_verification(store: Store, cs: sqlite3.Row, decision: dict[str, Any], *, target_id: str, merged_snapshot: str, pre_merge_snapshot: str, actor: str) -> str | None:
    plan = decision.get("post_merge_verification")
    if not plan:
        return None
    verification_id = new_id("verify")
    store.conn.execute(
        "INSERT INTO verifications (id, project_id, changeset_id, target_id, merged_snapshot_id, pre_merge_snapshot_id, check_set_id, merge_rule_id, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (verification_id, cs["project_id"], cs["id"], target_id, merged_snapshot, pre_merge_snapshot, plan["check_set"], plan["merge_rule"], actor, utcnow()),
    )
    store.create_event(
        kind="verification.queued",
        actor=actor,
        project_id=cs["project_id"],
        object_refs={"verification": verification_id, "changeset": cs["id"], "snapshot": merged_snapshot, "check_set": plan["check_set"]},
    )
    return verification_id


def snapshot_without(store: Store, project_id: str, snapshot_id: str, reverts: list[sqlite3.Row], *, actor: str) -> str:
    """`snapshot_id` with earlier reverted changesets undone, so a later
    verification is not blamed for a failure an earlier merge caused."""
    if not reverts:
        return snapshot_id
    entries = store.load_snapshot_tree(snapshot_id)
    for revert in reverts:
        undone, conflicts, _ = merge_entries(store, store.load_snapshot_tree(revert["merged_snapshot_id"]), entries, store.load_snapshot_tree(revert["pre_merge_snapshot_id"]))
        if not conflicts:
            entries = undone
    if entries == store.load_snapshot_tree(snapshot_id):
        return snapshot_id
    with store.conn:
        snapshot = store.create_snapshot(project_id, entries, parents=[snapshot_id], source="merge", actor=actor, metadata={"operation": "verify_candidate", "without": [revert["changeset_id"] for revert in reverts]})
    return snapshot["id"]


def revert_on_target(store: Store, verification: sqlite3.Row, *, actor: str, retries: int = 5) -> tuple[str | None, list[dict[str, Any]]]:
    for _attempt in range(retries + 1):
        target = store.row("SELECT * FROM targets WHERE id = ?", (verification["target_id"],))
        current = target["snapshot_id"]
        reverted, conflicts, _ = merge_entries(
            store,
            store.load_snapshot_tree(verification["merged_snapshot_id"]),
            store.load_snapshot_tree(current),
            store.load_snapshot_tree(verification["pre_merge_snapshot_id"]),
        )
        if conflicts:
            return None, conflicts
        cs = store.get_changeset(verification["changeset_id"])
        try:
            snapshot = _commit_revert(store, verification, cs, target, current, reverted, actor=actor)
        except _RetryRevert:
            continue
        return snapshot, []
    raise TriError("stale_target_snapshot", "Target kept moving while reverting a failed verification.", object=verification["target_id"], retryable=True)


def _commit_revert(store: Store, verification: sqlite3.Row, cs: sqlite3.Row, target: sqlite3.Row, current: str, reverted: dict[str, dict[str, Any]], *, actor: str) -> str:
        with store.conn:
            snapshot = store.create_snapshot(
                verification["project_id"],
                reverted,
                parents=[current],
                source="merge",
                actor=actor,
                metadata={"target": target["name"], "operation": "post_merge_revert", "reverts_changeset": cs["id"], "verification": verification["id"]},
            )
            updated = store.conn.execute("UPDATE targets SET snapshot_id = ? WHERE id = ? AND snapshot_id = ?", (snapshot["id"], target["id"], current))
            if updated.rowcount != 1:
                raise _RetryRevert()
            store.conn.execute("UPDATE changesets SET status = 'reverted' WHERE id = ?", (cs["id"],))
            if cs["workspace_id"]:
                store.conn.execute("UPDATE workspaces SET status = 'active', updated_at = ? WHERE id = ? AND status = 'promoted'", (utcnow(), cs["workspace_id"]))
            store.create_event(
                kind="changeset.reverted",
                actor=actor,
                project_id=verification["project_id"],
                workspace_id=cs["workspace_id"],
                object_refs={"changeset": cs["id"], "target": target["id"], "target_name": target["name"], "snapshot": snapshot["id"], "previous_snapshot": current, "verification": verification["id"]},
            )
        return snapshot["id"]


class _RetryRevert(Exception):
    pass


def run_one_verification(store: Store, verification: sqlite3.Row, *, actor: str, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(verification["changeset_id"])
    check_set = store.row("SELECT * FROM check_sets WHERE id = ?", (verification["check_set_id"],))
    specs = loads(check_set["required_checks_json"], []) if check_set else []
    reverts = store.rows(
        "SELECT * FROM verifications WHERE target_id = ? AND status = 'reverted' AND created_at <= ? AND rowid < (SELECT rowid FROM verifications WHERE id = ?) ORDER BY rowid",
        (verification["target_id"], verification["created_at"], verification["id"]),
    )
    candidate = snapshot_without(store, verification["project_id"], verification["merged_snapshot_id"], list(reverts), actor=actor)
    checks: list[dict[str, Any]] = []
    for spec in specs:
        check, _events = run_check_for_changeset_head(store, cs, spec["command"], actor=actor, command=command, start=start, snapshot=candidate)
        checks.append({"id": check["id"], "command": spec["command"], "status": check["status"]})
        if check["status"] != "passed":
            break
    passed = bool(specs) and all(check["status"] == "passed" for check in checks)
    result: dict[str, Any] = {"tested_snapshot": candidate, "without": [row["changeset_id"] for row in reverts]}
    revert_snapshot = None
    if passed:
        status = "verified"
    else:
        try:
            revert_snapshot, conflicts = revert_on_target(store, verification, actor=actor)
        except _RetryRevert:
            revert_snapshot, conflicts = None, []
            result["error"] = TriError("stale_target_snapshot", "Target moved during revert.", retryable=True).diagnostic
        if revert_snapshot:
            status = "reverted"
        else:
            status = "revert_conflict"
            result["conflicts"] = [{"kind": item["kind"], "path": item["path"]} for item in conflicts]
    with store.conn:
        store.conn.execute(
            "UPDATE verifications SET status = ?, checks_json = ?, revert_snapshot_id = ?, result_json = ?, completed_at = ? WHERE id = ?",
            (status, dumps(checks), revert_snapshot, dumps(result), utcnow(), verification["id"]),
        )
        store.create_event(
            kind=f"verification.{status}",
            actor=actor,
            project_id=verification["project_id"],
            object_refs={"verification": verification["id"], "changeset": cs["id"], "checks": [check["id"] for check in checks], "revert_snapshot": revert_snapshot},
        )
    return verification_data(store, resolve_verification(store, verification["id"]))


def cmd_verify_run(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if not set(VERIFY_ROLES) & set(actor_selectors(args.actor)):
        raise TriError("policy_denied", "Only maintainers or services can run post-merge verification.", object=args.project, action="verification.run", required_grant="role:maintainer")
    project = store.get_project(args.project)
    processed: list[dict[str, Any]] = []
    cycle = 0
    while True:
        cycle += 1
        rows = store.rows("SELECT * FROM verifications WHERE project_id = ? AND status = 'pending' ORDER BY rowid LIMIT ?", (project["id"], max(args.limit, 1)))
        for row in rows:
            claimed = False
            with store.conn:
                claimed = store.conn.execute("UPDATE verifications SET status = 'running' WHERE id = ? AND status = 'pending'", (row["id"],)).rowcount == 1
            if not claimed:
                continue
            try:
                processed.append(run_one_verification(store, resolve_verification(store, row["id"]), actor=args.actor, command=command, start=start))
            except Exception:
                with store.conn:
                    store.conn.execute("UPDATE verifications SET status = 'pending' WHERE id = ? AND status = 'running'", (row["id"],))
                raise
        if not args.watch or (args.max_cycles and cycle >= args.max_cycles):
            break
        time.sleep(max(args.interval, 0.05))
    pending = count_row(store, "SELECT COUNT(*) AS count FROM verifications WHERE project_id = ? AND status = 'pending'", (project["id"],))
    return envelope("tri.verification.run", command, actor=args.actor, start=start, data={"project": project["id"], "processed": processed, "pending": pending, "cycles": cycle})


def cmd_verify_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    if args.status:
        rows = store.rows("SELECT * FROM verifications WHERE project_id = ? AND status = ? ORDER BY rowid", (project["id"], args.status))
    else:
        rows = store.rows("SELECT * FROM verifications WHERE project_id = ? ORDER BY rowid", (project["id"],))
    visible = [row for row in rows if object_access_decision(store, row["changeset_id"], actor=args.actor, action="changeset.read")[0]["allowed"]]
    return envelope("tri.verification.list", command, actor=args.actor, start=start, data={"project": project["id"], "verifications": [verification_data(store, row) for row in visible], "redacted": len(rows) - len(visible)})


def cmd_verify_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = resolve_verification(store, args.verification)
    require_object_access(store, row["changeset_id"], actor=args.actor, action="changeset.read")
    return envelope("tri.verification.inspect", command, actor=args.actor, start=start, data=verification_data(store, row))


# --- Two-way Git mirror -------------------------------------------------------
#
# A mirror pairs one Tri target with one Git branch. Each sync records a pair
# (synced_snapshot, synced_git_commit) whose mirrorable trees are identical.
# Paths the mirror audience cannot read (hidden packages) and secret-like paths
# are not mirrorable: Git never receives Tri's copy and Tri never takes Git's,
# so each side keeps what the other cannot see.

MIRROR_TARGET_EVENTS = ("changeset.merged", "target.refreshed", "changeset.reverted")
DEFAULT_MIRROR_AUDIENCE = "service:git-mirror"


def mirror_git_dir(store: Store, mirror_id: str) -> Path:
    return store.home / "mirrors" / f"{mirror_id}.git"


def mirror_error(exc: gitmirror.GitMirrorError) -> TriError:
    return TriError(exc.code, exc.message, retryable=exc.retryable, **exc.fields)


def mirror_data(store: Store, row: sqlite3.Row) -> dict[str, Any]:
    target = store.row("SELECT name, snapshot_id FROM targets WHERE id = ?", (row["target_id"],))
    return {
        "id": row["id"],
        "project": row["project_id"],
        "target": target["name"] if target else None,
        "target_snapshot": target["snapshot_id"] if target else None,
        "remote": row["remote"],
        "branch": row["branch"],
        "audience": row["audience"],
        "prefer": row["prefer"],
        "status": row["status"],
        "synced_snapshot": row["synced_snapshot_id"],
        "synced_git_commit": row["synced_git_commit"],
        "in_sync": bool(target) and row["synced_snapshot_id"] == target["snapshot_id"],
        "last_sync_at": row["last_sync_at"],
        "last_result": loads(row["last_result_json"], {}),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "removed_at": row["removed_at"],
    }


def resolve_mirror(store: Store, mirror_ref: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM mirrors WHERE id = ?", (mirror_ref,))
    if row is None:
        project = store.row("SELECT id FROM projects WHERE id = ? OR name = ?", (mirror_ref, mirror_ref))
        if project is not None:
            row = store.row("SELECT * FROM mirrors WHERE project_id = ? AND status = 'active' ORDER BY created_at LIMIT 1", (project["id"],))
    if row is None:
        raise TriError("path_not_found", "Mirror not found.", object=mirror_ref)
    return row


def mirror_is_mirrorable(path: str, hidden: list[str]) -> bool:
    return not is_secret_like_path(path) and not path_matches_hidden(path, hidden)


def mirror_entry_from_git(store: Store, mode: str, content: bytes) -> dict[str, Any]:
    blob = store.put_blob(content)
    entry = {"blob": blob["id"], "hash": blob["hash"], "size": blob["size"], "mode": mode}
    if mode == "120000":
        entry["symlink_target"] = content.decode(errors="replace")
    return entry


def mirror_author_actor(info: gitmirror.CommitInfo, fallback: str) -> str:
    return actor_ref_from_git_author(info.author_name, info.author_email) or fallback


def mirror_actor_ident(actor: str) -> tuple[str, str]:
    slug = re.sub(r"[^a-z0-9._-]+", "-", actor.lower()).strip("-") or "tri"
    return actor, f"{slug}@tri.invalid"


def mirror_timestamp(value: str | None) -> int:
    if not value:
        return int(time.time())
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(time.time())


def mirror_inbound_entries(
    store: Store,
    git_dir: Path,
    *,
    previous: dict[str, dict[str, Any]],
    previous_commit: str | None,
    commit: str,
    hidden: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Tri entries for a Git commit: Git's mirrorable paths, Tri's copy of everything else."""
    ignored: list[str] = []
    if previous_commit is not None:
        entries = dict(previous)
        changes = gitmirror.diff_tree(git_dir, previous_commit, commit)
        wanted = []
        for change in changes:
            if not mirror_is_mirrorable(change.path, hidden):
                ignored.append(change.path)
            elif change.status == "D":
                entries.pop(normalize_path(change.path), None)
            elif change.mode == gitmirror.SUBMODULE_MODE:
                ignored.append(change.path)
            else:
                wanted.append(change)
        blobs = git_cat_file_batch(git_dir, sorted({change.oid for change in wanted if change.oid}))
        for change in wanted:
            entries[normalize_path(change.path)] = mirror_entry_from_git(store, change.mode or "100644", blobs[change.oid or ""])
        return dict(sorted(entries.items())), sorted(set(ignored))
    tree = gitmirror.ls_tree(git_dir, commit)
    entries = {path: entry for path, entry in previous.items() if not mirror_is_mirrorable(path, hidden)}
    keep = {}
    for path, item in tree.items():
        if not mirror_is_mirrorable(path, hidden):
            ignored.append(path)
            continue
        keep[path] = item
    blobs = git_cat_file_batch(git_dir, sorted({item.oid for item in keep.values()}))
    for path, item in keep.items():
        entries[normalize_path(path)] = mirror_entry_from_git(store, item.mode, blobs[item.oid])
    return dict(sorted(entries.items())), sorted(set(ignored))


def mirror_tree_changes(
    store: Store,
    old: dict[str, dict[str, Any]],
    new: dict[str, dict[str, Any]],
    hidden: list[str],
) -> tuple[dict[str, object], dict[str, str]]:
    changes: dict[str, object] = {}
    modes: dict[str, str] = {}
    for path in changed_paths(old, new):
        if not mirror_is_mirrorable(path, hidden):
            continue
        entry = new.get(path)
        if entry is None:
            changes[path] = None
            continue
        changes[path] = store.read_blob(entry["blob"])
        modes[path] = entry.get("mode") or "100644"
    return changes, modes


def mirror_tri_steps(store: Store, mirror: sqlite3.Row, target_id: str, target_snapshot: str) -> tuple[list[dict[str, Any]], int]:
    """Target moves recorded since the last sync, oldest first, ending at the current target."""
    steps: list[dict[str, Any]] = []
    cursor = int(mirror["event_cursor"] or 0)
    placeholders = ",".join("?" for _ in MIRROR_TARGET_EVENTS)
    rows = store.rows(
        f"SELECT rowid, * FROM events WHERE rowid > ? AND project_id = ? AND kind IN ({placeholders}) ORDER BY rowid",
        (cursor, mirror["project_id"], *MIRROR_TARGET_EVENTS),
    )
    for row in rows:
        refs = loads(row["object_refs_json"], {})
        if refs.get("target") != target_id or not refs.get("snapshot"):
            continue
        if steps and steps[-1]["snapshot"] == refs["snapshot"]:
            continue
        title = None
        if refs.get("changeset"):
            cs = store.row("SELECT title FROM changesets WHERE id = ?", (refs["changeset"],))
            title = cs["title"] if cs else None
        if row["kind"] == "changeset.reverted" and title:
            title = f"Revert \"{title}\" (post-merge checks failed)"
        steps.append({"snapshot": refs["snapshot"], "actor": row["actor"], "at": row["created_at"], "changeset": refs.get("changeset"), "title": title, "kind": row["kind"]})
        cursor = int(row["rowid"])
        if refs["snapshot"] == target_snapshot:
            break
    if not steps or steps[-1]["snapshot"] != target_snapshot:
        steps.append({"snapshot": target_snapshot, "actor": None, "at": None, "changeset": None, "title": None, "kind": "target"})
    return steps, cursor


def mirror_step_message(step: dict[str, Any]) -> str:
    title = step.get("title") or ("Refresh Tri target" if step.get("kind") == "target.refreshed" else "Sync Tri target")
    trailers = [f"Tri-Snapshot: {step['snapshot']}"]
    if step.get("changeset"):
        trailers.insert(0, f"Tri-Changeset: {step['changeset']}")
    return f"{title}\n\n" + "\n".join(trailers) + "\n"


def mirror_record_commit(store: Store, mirror_id: str, snapshot_id: str, commit: str, direction: str) -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO mirror_commits (mirror_id, snapshot_id, git_commit, direction, created_at) VALUES (?, ?, ?, ?, ?)",
        (mirror_id, snapshot_id, commit, direction, utcnow()),
    )


def mirror_sync_once(store: Store, mirror: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    git_dir = mirror_git_dir(store, mirror["id"])
    try:
        gitmirror.ensure_bare(git_dir)
        remote_tip = gitmirror.fetch(git_dir, mirror["remote"], mirror["branch"])
    except gitmirror.GitMirrorError as exc:
        raise mirror_error(exc) from exc
    target = store.row("SELECT * FROM targets WHERE id = ?", (mirror["target_id"],))
    if target is None:
        raise TriError("path_not_found", "Mirrored target no longer exists.", object=mirror["target_id"])
    target_snapshot = target["snapshot_id"]
    synced_snapshot = mirror["synced_snapshot_id"]
    synced_commit = mirror["synced_git_commit"]
    hidden = effective_hidden_patterns(store, mirror["project_id"], [], actor=mirror["audience"])
    warnings: list[dict[str, Any]] = []
    result: dict[str, Any] = {"mode": "noop", "inbound_commits": [], "inbound_snapshots": [], "outbound_commits": [], "ignored_git_paths": [], "text_merged_paths": []}

    try:
        if synced_commit is not None and remote_tip is not None and not gitmirror.has_commit(git_dir, synced_commit):
            synced_commit_known = False
        else:
            synced_commit_known = True

        # Inbound: Git commits after the synced commit become Tri snapshots.
        inbound_tip = synced_snapshot
        inbound_parent = synced_snapshot or target_snapshot
        inbound_commit = synced_commit
        inbound_rows: list[tuple[str, str]] = []
        with store.conn:
            if remote_tip is not None and remote_tip != synced_commit:
                if synced_commit is not None and synced_commit_known and gitmirror.is_ancestor(git_dir, synced_commit, remote_tip):
                    commits = gitmirror.first_parent_commits(git_dir, remote_tip, synced_commit)
                    previous_commit: str | None = synced_commit
                else:
                    if synced_commit is not None:
                        warnings.append(TriError("mirror_history_rewritten", "The Git branch was force-pushed; mirroring its tip as one change.", severity="warning", remote_tip=remote_tip, synced_git_commit=synced_commit).diagnostic)
                    commits = [remote_tip]
                    previous_commit = None
                base_for_hidden = synced_snapshot or target_snapshot
                entries = store.load_snapshot_tree(base_for_hidden) if base_for_hidden else {}
                for commit in commits:
                    entries, ignored = mirror_inbound_entries(store, git_dir, previous=entries, previous_commit=previous_commit, commit=commit, hidden=hidden)
                    result["ignored_git_paths"].extend(ignored)
                    info = gitmirror.commit_info(git_dir, commit)
                    if synced_snapshot is None and commit == remote_tip and not changed_paths(store.load_snapshot_tree(target_snapshot), entries):
                        inbound_tip = target_snapshot  # first sync of identical trees: pair them without a new snapshot
                    else:
                        snapshot = store.create_snapshot(
                            mirror["project_id"],
                            entries,
                            parents=[inbound_parent] if inbound_parent else [],
                            source="imported",
                            actor=mirror_author_actor(info, actor),
                            metadata={
                                "mirror": mirror["id"],
                                "source_path": mirror["remote"],
                                "import_ref": mirror["branch"],
                                "git_commit": commit,
                                "git_tree": info.tree,
                                "git_author": {"name": info.author_name, "email": info.author_email, "date": info.author_date, "actor": mirror_author_actor(info, actor)},
                                "git_message": info.message[:2000],
                            },
                        )
                        inbound_tip = snapshot["id"]
                        inbound_parent = inbound_tip
                        result["inbound_snapshots"].append(inbound_tip)
                    inbound_rows.append((inbound_tip, commit))
                    result["inbound_commits"].append(commit)
                    previous_commit = commit
                inbound_commit = remote_tip
            result["ignored_git_paths"] = sorted(set(result["ignored_git_paths"]))

            # Decide the new target and whether Tri has work Git lacks.
            merge_snapshot: str | None = None
            if synced_snapshot is None:
                if remote_tip is None:
                    new_target, outbound_from = target_snapshot, None
                    result["mode"] = "initial_push"
                elif inbound_tip == target_snapshot:
                    new_target, outbound_from = target_snapshot, None
                    result["mode"] = "initial_pair"
                elif mirror["prefer"] == "git":
                    new_target, outbound_from = inbound_tip, None
                    result["mode"] = "initial_adopt_git"
                elif mirror["prefer"] == "tri":
                    new_target, outbound_from = target_snapshot, inbound_tip
                    result["mode"] = "initial_adopt_tri"
                else:
                    differing = changed_paths(store.load_snapshot_tree(target_snapshot), store.load_snapshot_tree(inbound_tip))
                    raise TriError(
                        "mirror_diverged",
                        "The Git branch and the Tri target differ; add the mirror with --prefer git or --prefer tri to choose the starting content.",
                        object=mirror["id"],
                        differing_paths=len(differing),
                        retryable=False,
                    )
            elif not inbound_rows:
                new_target, outbound_from = target_snapshot, (synced_snapshot if target_snapshot != synced_snapshot else None)
                result["mode"] = "outbound" if outbound_from else "noop"
            elif target_snapshot == synced_snapshot:
                new_target, outbound_from = inbound_tip, None
                result["mode"] = "inbound"
            else:
                text_merged: list[str] = []
                merged, conflicts, _ = merge_entries(
                    store,
                    store.load_snapshot_tree(synced_snapshot),
                    store.load_snapshot_tree(target_snapshot),
                    store.load_snapshot_tree(inbound_tip),
                    text_merged,
                )
                if conflicts:
                    raise TriError(
                        "mirror_conflict",
                        "Tri and Git changed the same lines since the last sync; resolve in a workspace and merge, then sync again.",
                        object=mirror["id"],
                        conflicts=[{"kind": item["kind"], "path": item["path"]} for item in conflicts],
                        retryable=False,
                    )
                snapshot = store.create_snapshot(
                    mirror["project_id"],
                    merged,
                    parents=[target_snapshot, inbound_tip],
                    source="merge",
                    actor=actor,
                    metadata={"target": target["name"], "operation": "mirror_merge", "mirror": mirror["id"], "git_commit": remote_tip, "text_merged_paths": text_merged},
                )
                merge_snapshot = snapshot["id"]
                result["text_merged_paths"] = text_merged
                new_target, outbound_from = merge_snapshot, synced_snapshot
                result["mode"] = "merge"

            if new_target != target_snapshot:
                updated = store.conn.execute("UPDATE targets SET snapshot_id = ? WHERE id = ? AND snapshot_id = ?", (new_target, target["id"], target_snapshot))
                if updated.rowcount != 1:
                    raise TriError("stale_target_snapshot", "Target changed during mirror sync.", object=target["name"], expected=target_snapshot, retryable=True)
                store.create_event(
                    kind="mirror.inbound",
                    actor=actor,
                    project_id=mirror["project_id"],
                    object_refs={"mirror": mirror["id"], "target": target["id"], "target_name": target["name"], "snapshot": new_target, "previous_snapshot": target_snapshot, "git_commit": remote_tip},
                )
            for snapshot_id, commit in inbound_rows:
                mirror_record_commit(store, mirror["id"], snapshot_id, commit, "inbound")
            if outbound_from is None and new_target is not None and (inbound_commit or remote_tip):
                # Git is now the full story: record the exact pair.
                pair_commit = inbound_commit or remote_tip
                store.conn.execute(
                    "UPDATE mirrors SET synced_snapshot_id = ?, synced_git_commit = ?, event_cursor = (SELECT COALESCE(MAX(rowid), 0) FROM events) WHERE id = ?",
                    (new_target, pair_commit, mirror["id"]),
                )
                mirror_record_commit(store, mirror["id"], new_target, pair_commit, "pair")

        # Outbound: Tri target moves since the pair become Git commits, then one push.
        needs_push = outbound_from is not None or (synced_snapshot is None and remote_tip is None)
        if needs_push:
            commits_to_write: list[gitmirror.OutboundCommit] = []
            step_snapshots: list[str] = []
            if synced_snapshot is None and remote_tip is None:
                entries = store.load_snapshot_tree(target_snapshot)
                changes, modes = mirror_tree_changes(store, {}, entries, hidden)
                name, email = mirror_actor_ident(actor)
                commits_to_write.append(gitmirror.OutboundCommit(parents=[], message=f"Import Tri target {target['name']}\n\nTri-Snapshot: {target_snapshot}\n", author=(name, email, int(time.time())), changes=changes, modes=modes))
                step_snapshots.append(target_snapshot)
                cursor = int(store.row("SELECT COALESCE(MAX(rowid), 0) AS cursor FROM events", ())["cursor"])
            elif synced_snapshot is None:
                # --prefer tri on a non-empty branch: one commit on top of the Git tip.
                assert remote_tip is not None and outbound_from is not None
                changes, modes = mirror_tree_changes(store, store.load_snapshot_tree(outbound_from), store.load_snapshot_tree(target_snapshot), hidden)
                name, email = mirror_actor_ident(actor)
                commits_to_write.append(gitmirror.OutboundCommit(parents=[remote_tip], message=f"Adopt Tri target {target['name']}\n\nTri-Snapshot: {target_snapshot}\n", author=(name, email, int(time.time())), changes=changes, modes=modes))
                step_snapshots.append(target_snapshot)
                cursor = int(store.row("SELECT COALESCE(MAX(rowid), 0) AS cursor FROM events", ())["cursor"])
            else:
                assert synced_commit is not None
                steps, cursor = mirror_tri_steps(store, mirror, target["id"], target_snapshot)
                parent_ref = synced_commit
                parent_entries = store.load_snapshot_tree(synced_snapshot)
                for step in steps:
                    step_entries = store.load_snapshot_tree(step["snapshot"])
                    changes, modes = mirror_tree_changes(store, parent_entries, step_entries, hidden)
                    parent_entries = step_entries
                    if not changes:
                        continue
                    name, email = mirror_actor_ident(step["actor"] or actor)
                    commits_to_write.append(gitmirror.OutboundCommit(parents=[parent_ref], message=mirror_step_message(step), author=(name, email, mirror_timestamp(step["at"])), changes=changes, modes=modes))
                    step_snapshots.append(step["snapshot"])
                    parent_ref = f"mark:{len(commits_to_write)}"
                if merge_snapshot is not None:
                    assert remote_tip is not None
                    changes, modes = mirror_tree_changes(store, store.load_snapshot_tree(target_snapshot), store.load_snapshot_tree(merge_snapshot), hidden)
                    for change in gitmirror.diff_tree(git_dir, synced_commit, remote_tip) if synced_commit_known else []:
                        if not mirror_is_mirrorable(change.path, hidden):
                            changes[change.path] = None if change.status == "D" else (change.mode, change.oid)
                    name, email = mirror_actor_ident(actor)
                    commits_to_write.append(
                        gitmirror.OutboundCommit(
                            parents=[parent_ref, remote_tip],
                            message=f"Merge Git branch {mirror['branch']} into Tri target {target['name']}\n\nTri-Snapshot: {merge_snapshot}\n",
                            author=(name, email, int(time.time())),
                            changes=changes,
                            modes=modes,
                        )
                    )
                    step_snapshots.append(merge_snapshot)
            if commits_to_write:
                shas = gitmirror.fast_import(git_dir, commits_to_write)
                gitmirror.push(git_dir, mirror["remote"], shas[-1], mirror["branch"])
                pair_commit = shas[-1]
            else:
                # Only non-mirrorable paths changed: Git already matches.
                shas = []
                pair_commit = remote_tip or synced_commit
            pair_snapshot = merge_snapshot or target_snapshot
            with store.conn:
                for snapshot_id, sha in zip(step_snapshots, shas):
                    mirror_record_commit(store, mirror["id"], snapshot_id, sha, "outbound")
                store.conn.execute(
                    "UPDATE mirrors SET synced_snapshot_id = ?, synced_git_commit = ?, event_cursor = MAX(event_cursor, ?) WHERE id = ?",
                    (pair_snapshot, pair_commit, cursor, mirror["id"]),
                )
                mirror_record_commit(store, mirror["id"], pair_snapshot, pair_commit, "pair")
            result["outbound_commits"] = shas
    except gitmirror.GitMirrorError as exc:
        raise mirror_error(exc) from exc
    result["warnings"] = warnings
    return result


def mirror_save_result(store: Store, mirror_id: str, result: dict[str, Any]) -> None:
    with store.conn:
        store.conn.execute("UPDATE mirrors SET last_sync_at = ?, last_result_json = ? WHERE id = ?", (utcnow(), dumps(result), mirror_id))


def mirror_sync_with_retry(store: Store, mirror_id: str, *, actor: str, retries: int) -> tuple[dict[str, Any], int]:
    attempt = 0
    while True:
        attempt += 1
        mirror = store.row("SELECT * FROM mirrors WHERE id = ?", (mirror_id,))
        try:
            return mirror_sync_once(store, mirror, actor=actor), attempt
        except TriError as exc:
            if not exc.diagnostic.get("retryable") or attempt > retries:
                raise
            time.sleep(min(0.1 * attempt, 1.0))


def cmd_mirror_add(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_maintainer(args.actor, action="mirror.create", object_id=args.project, message="Only maintainers can mirror a target to Git.")
    project = store.get_project(args.project)
    target = resolve_target_for_project(store, project["id"], args.target)
    if store.row("SELECT id FROM mirrors WHERE target_id = ? AND status = 'active'", (target["id"],)):
        raise TriError("invalid_command", "This target already has an active mirror.", object=target["name"])
    remote = args.remote
    if "://" not in remote and not remote.startswith("git@") and Path(remote).exists():
        remote = str(Path(remote).resolve())
    mirror_id = new_id("mirror")
    with store.conn:
        store.conn.execute(
            "INSERT INTO mirrors (id, project_id, target_id, remote, branch, audience, prefer, status, event_cursor, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)",
            (mirror_id, project["id"], target["id"], remote, args.branch or target["name"], args.audience or DEFAULT_MIRROR_AUDIENCE, args.prefer, args.actor, utcnow()),
        )
        evt = store.create_event(kind="mirror.created", actor=args.actor, project_id=project["id"], object_refs={"mirror": mirror_id, "target": target["id"], "remote": remote})
    events = [evt]
    diagnostics: list[dict[str, Any]] = []
    if not args.no_sync:
        try:
            result, _ = mirror_sync_with_retry(store, mirror_id, actor=args.actor, retries=args.retry)
            mirror_save_result(store, mirror_id, result)
        except TriError as exc:
            mirror_save_result(store, mirror_id, {"error": exc.diagnostic})
            diagnostics.append(exc.diagnostic)
    data = mirror_data(store, store.row("SELECT * FROM mirrors WHERE id = ?", (mirror_id,)))
    return envelope("tri.mirror.inspect", command, actor=args.actor, start=start, ok=not diagnostics, events=events, diagnostics=diagnostics, data=data)


def cmd_mirror_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.project:
        project = store.get_project(args.project)
        rows = store.rows("SELECT * FROM mirrors WHERE project_id = ? ORDER BY created_at, rowid", (project["id"],))
    else:
        rows = store.rows("SELECT * FROM mirrors ORDER BY created_at, rowid")
    mirrors = [mirror_data(store, row) for row in rows if args.all or row["status"] == "active"]
    return envelope("tri.mirror.list", command, actor=args.actor, start=start, data={"mirrors": mirrors})


def cmd_mirror_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.mirror.inspect", command, actor=args.actor, start=start, data=mirror_data(store, resolve_mirror(store, args.mirror)))


def cmd_mirror_remove(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = resolve_mirror(store, args.mirror)
    require_maintainer(args.actor, action="mirror.remove", object_id=row["id"], message="Only maintainers can remove a mirror.")
    with store.conn:
        store.conn.execute("UPDATE mirrors SET status = 'removed', removed_at = ? WHERE id = ?", (utcnow(), row["id"]))
        evt = store.create_event(kind="mirror.removed", actor=args.actor, project_id=row["project_id"], object_refs={"mirror": row["id"]})
    shutil.rmtree(mirror_git_dir(store, row["id"]), ignore_errors=True)
    return envelope("tri.mirror.inspect", command, actor=args.actor, start=start, events=[evt], data=mirror_data(store, store.row("SELECT * FROM mirrors WHERE id = ?", (row["id"],))))


def cmd_mirror_sync(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = resolve_mirror(store, args.mirror)
    require_maintainer(args.actor, action="mirror.sync", object_id=row["id"], message="Only maintainers can sync a mirror.")
    if row["status"] != "active":
        raise TriError("invalid_command", "Mirror was removed.", object=row["id"])
    cycles: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    cycle = 0
    while True:
        cycle += 1
        try:
            result, attempts = mirror_sync_with_retry(store, row["id"], actor=args.actor, retries=args.retry)
            result["attempts"] = attempts
            mirror_save_result(store, row["id"], result)
            cycles.append(result)
            diagnostics = []
        except TriError as exc:
            mirror_save_result(store, row["id"], {"error": exc.diagnostic})
            diagnostics = [exc.diagnostic]
            if not args.watch or not exc.diagnostic.get("retryable"):
                break
        if not args.watch or (args.max_cycles and cycle >= args.max_cycles):
            break
        time.sleep(max(args.interval, 0.05))
    data = mirror_data(store, store.row("SELECT * FROM mirrors WHERE id = ?", (row["id"],)))
    data["sync"] = cycles[-1] if cycles else None
    data["cycles"] = cycle
    warnings = cycles[-1].get("warnings", []) if cycles else []
    return envelope("tri.mirror.sync", command, actor=args.actor, start=start, ok=not diagnostics, diagnostics=diagnostics, warnings=warnings, data=data)


def cmd_check_set_create(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can create project check sets.", object=args.project, action="check_set.create", required_grant="role:maintainer")
    project = store.get_project(args.project)
    required_checks = parse_required_checks(args.required_check or [])
    if not required_checks:
        raise TriError("invalid_command", "A check set requires at least one --required-check.")
    check_set_id = new_id("checkset")
    with store.conn:
        existing = store.row("SELECT id FROM check_sets WHERE project_id = ? AND name = ?", (project["id"], args.name))
        if existing is not None:
            raise TriError("invalid_command", "A check set with this name already exists.", object=args.name)
        store.conn.execute(
            "INSERT INTO check_sets (id, project_id, name, required_checks_json, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (check_set_id, project["id"], args.name, dumps(required_checks), args.actor, utcnow()),
        )
        evt = store.create_event(
            kind="check_set.created",
            actor=args.actor,
            project_id=project["id"],
            object_refs={"check_set": check_set_id, "name": args.name, "required_checks": required_checks},
        )
    return envelope("tri.check_set.inspect", command, actor=args.actor, start=start, events=[evt], data=check_set_data(resolve_check_set(store, project["id"], check_set_id)))


def cmd_check_set_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    check_sets = [check_set_data(row) for row in store.rows("SELECT * FROM check_sets WHERE project_id = ? ORDER BY name", (project["id"],))]
    return envelope("tri.check_set.list", command, actor=args.actor, start=start, data={"project": project["id"], "check_sets": check_sets})


def cmd_check_set_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.check_set.inspect", command, actor=args.actor, start=start, data=check_set_data(resolve_check_set_for_inspect(store, args.project, args.name)))


def default_changeset_title(store: Store, ws: sqlite3.Row) -> str:
    if ws["task_id"]:
        task = store.row("SELECT title FROM tasks WHERE id = ?", (ws["task_id"],))
        if task and task["title"]:
            return task["title"]
    if ws["advisory_id"]:
        advisory = store.row("SELECT title FROM advisories WHERE id = ?", (ws["advisory_id"],))
        if advisory and advisory["title"]:
            return advisory["title"]
    return f"Changes from {ws['id']}"


def cmd_changeset_create(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can create changesets.", object=ws["id"], status=ws["status"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    ops = store.rows("SELECT id, path_set_json FROM operations WHERE workspace_id = ? ORDER BY created_at, rowid", (ws["id"],))
    op_ids = [row["id"] for row in ops]
    touched = sorted({path for row in ops for path in loads(row["path_set_json"], [])})
    checks = [row["id"] for row in store.rows("SELECT id FROM check_runs WHERE workspace_id = ? ORDER BY created_at", (ws["id"],))]
    hidden_paths = sorted(set(loads(ws["hidden_paths_json"], []) + normalize_hidden_patterns(args.hide_path) + package_hidden_patterns(store, ws["project_id"], args.hide_package, actor=args.actor)))
    effective_hidden = effective_hidden_patterns(store, ws["project_id"], hidden_paths, actor=args.actor)
    visible_touched_for_suggestions = [path for path in touched if path_visibility(path, effective_hidden)[0]]
    suggested = [{"kind": "path", "paths": ["test/**"], "reason": "test-only changes can review separately"}] if any(path.startswith("test/") for path in visible_touched_for_suggestions) else []
    suggested.extend(package_split_points_for_paths(store, ws["project_id"], visible_touched_for_suggestions))
    advisory_id = ws["advisory_id"]
    visibility = args.visibility or (["embargoed"] if advisory_id else loads(ws["visibility_json"], []))
    task_run = latest_workspace_task_run(store, ws["id"])
    title = args.title or default_changeset_title(store, ws)
    cs_id = new_id("cs")
    with store.conn:
        store.conn.execute(
            """
            INSERT INTO changesets (
              id, project_id, workspace_id, title, base_snapshot_id, head_snapshot_id, status,
              operations_json, task_run_id, visibility_json, hidden_paths_json, touched_paths_json,
              checks_json, reviews_json, policy_decisions_json, export_status,
              stack_parent_id, advisory_id, replacement_changeset_id, lifecycle_json,
              suggested_split_points_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cs_id,
                ws["project_id"],
                ws["id"],
                title,
                ws["base_snapshot_id"],
                ws["head_snapshot_id"],
                "ready",
                dumps(op_ids),
                task_run["id"] if task_run else None,
                dumps(visibility),
                dumps(hidden_paths),
                dumps(touched),
                dumps(checks),
                dumps([]),
                dumps([]),
                "not_exported",
                None,
                advisory_id,
                None,
                dumps({"created_by": args.actor, "created_at": utcnow()}),
                dumps(suggested),
                utcnow(),
            ),
        )
        if advisory_id:
            advisory = store.get_advisory(advisory_id)
            private_changesets = loads(advisory["private_changesets_json"], [])
            if cs_id not in private_changesets:
                private_changesets.append(cs_id)
                store.conn.execute("UPDATE advisories SET private_changesets_json = ? WHERE id = ?", (dumps(private_changesets), advisory_id))
        phase_evt = set_workspace_task_run_phase(store, ws, actor=args.actor, status="summarizing", reason="changeset.create")
        evt = store.create_event(
            kind="changeset.created",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"changeset": cs_id, "workspace": ws["id"], "advisory": advisory_id, "hidden_packages": args.hide_package or []},
        )
    events = [evt] if phase_evt is None else [phase_evt, evt]
    return envelope("tri.changeset.inspect", command, actor=args.actor, start=start, events=events, data=changeset_data(store, cs_id, actor=args.actor))


def changeset_stack_data(store: Store, row: sqlite3.Row, *, actor: str | None = None) -> dict[str, Any]:
    parent_id = row["stack_parent_id"]
    revalidation_checks = []
    for check_id in loads(row["checks_json"], []):
        check = check_data(store, check_id)
        revalidation_checks.append(
            {
                "id": check["id"],
                "status": check["status"],
                "snapshot_after": check["snapshot_after"],
                "current_for_head": check["snapshot_after"] == row["head_snapshot_id"],
            }
        )
    stack = {
        "parent": parent_id,
        "base_snapshot": row["base_snapshot_id"],
        "dependency_state": "independent" if parent_id is None else "missing_parent",
        "parent_status": None,
        "parent_head_snapshot": None,
        "parent_redacted": False,
        "revalidation_checks": revalidation_checks,
    }
    if parent_id is None:
        return stack
    parent = store.row("SELECT id, status, head_snapshot_id FROM changesets WHERE id = ?", (parent_id,))
    if parent is None:
        return stack
    if actor is not None:
        decision, _ = object_access_decision(store, parent_id, actor=actor, action="changeset.read")
        if not decision["allowed"]:
            stack["parent"] = None
            stack["dependency_state"] = "redacted_parent"
            stack["parent_redacted"] = True
            return stack
    stack["parent_status"] = parent["status"]
    stack["parent_head_snapshot"] = parent["head_snapshot_id"]
    stack["dependency_state"] = "satisfied" if parent["status"] == "merged" else "pending_parent"
    return stack


def operation_detail_data(
    store: Store,
    operation_id: str,
    hidden_patterns: list[str],
    *,
    include_hidden: bool = False,
    workspace_visible: bool = True,
) -> dict[str, Any] | None:
    row = store.row("SELECT * FROM operations WHERE id = ?", (operation_id,))
    if row is None:
        return None
    paths = loads(row["path_set_json"], [])
    visible_paths = []
    redacted_paths = 0
    for path in paths:
        visible, _ = path_visibility(path, hidden_patterns)
        if include_hidden or visible:
            visible_paths.append(path)
        else:
            redacted_paths += 1
    metadata = loads(row["metadata_json"], {})
    def redact_metadata(value: Any, key: str | None = None) -> Any:
        if include_hidden:
            return value
        if isinstance(value, dict):
            return {item_key: redact_metadata(item_value, item_key) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [redact_metadata(item, key) for item in value]
        if isinstance(value, str) and key in {"path", "paths", "src", "dest"}:
            visible, reason = path_visibility(value, hidden_patterns)
            if not visible:
                return {**redacted_path_ref(value), "redaction": reason}
        return value

    def redact_policy(policy: dict[str, Any]) -> dict[str, Any]:
        if workspace_visible:
            return policy
        redacted = dict(policy)
        if redacted.get("object") == row["workspace_id"]:
            redacted["object"] = None
            redacted["object_redacted"] = True
        matched_grants = []
        for grant in redacted.get("matched_grants", []):
            matched = dict(grant)
            if matched.get("object") == row["workspace_id"]:
                matched["object"] = None
                matched["object_redacted"] = True
            if matched.get("inherited_from") == row["workspace_id"]:
                matched["inherited_from"] = None
                matched["inherited_from_redacted"] = True
            matched_grants.append(matched)
        redacted["matched_grants"] = matched_grants
        return redacted

    return {
        "id": row["id"],
        "workspace": row["workspace_id"] if workspace_visible else None,
        "workspace_redacted": not workspace_visible,
        "actor": row["actor"],
        "kind": row["kind"],
        "input_snapshot": row["input_snapshot_id"],
        "output_snapshot": row["output_snapshot_id"],
        "paths": paths if include_hidden else visible_paths,
        "redacted_paths": 0 if include_hidden else redacted_paths,
        "policy": redact_policy(metadata.get("policy", {})),
        "metadata": redact_metadata({key: value for key, value in metadata.items() if key != "policy"}),
        "created_at": row["created_at"],
    }


def operation_row(store: Store, operation_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM operations WHERE id = ?", (operation_id,))
    if row is None:
        raise TriError("path_not_found", "Operation not found.", object=operation_id)
    return row


def operation_access_context(store: Store, operation_id: str, *, actor: str) -> dict[str, Any]:
    row = operation_row(store, operation_id)
    workspace = store.get_workspace(row["workspace_id"])
    linked_changesets = linked_changesets_for_operation(store, operation_id)
    origins = []
    redacted_origins = 0
    hidden_patterns: set[str] = set()
    workspace_visible = False

    workspace_decision, _ = object_access_decision(store, row["workspace_id"], actor=actor, action="workspace.read")
    if workspace_decision["allowed"]:
        workspace_visible = True
        hidden_patterns.update(effective_hidden_patterns(store, workspace["project_id"], loads(workspace["hidden_paths_json"], []), actor=actor))
        origins.append(
            {
                "type": "workspace",
                "object": row["workspace_id"],
                "action": "workspace.read",
                "reason": workspace_decision["reason"],
                "grants": workspace_decision["grants"],
            }
        )
    else:
        redacted_origins += 1

    visible_changesets = []
    redacted_changesets = 0
    for changeset_id in linked_changesets:
        decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
        if decision["allowed"]:
            changeset = store.get_changeset(changeset_id)
            hidden_patterns.update(effective_hidden_patterns(store, changeset["project_id"], loads(changeset["hidden_paths_json"], []), actor=actor))
            visible_changesets.append(changeset_id)
            origins.append(
                {
                    "type": "changeset",
                    "object": changeset_id,
                    "action": "changeset.read",
                    "reason": decision["reason"],
                    "grants": decision["grants"],
                }
            )
        else:
            redacted_changesets += 1
            redacted_origins += 1

    allowed = bool(origins)
    if not allowed:
        required_grants = ["workspace.read"]
        if linked_changesets:
            required_grants.append("changeset.read")
        raise TriError(
            "policy_denied",
            "Actor is not allowed to access this operation.",
            object=operation_id,
            action="operation.read",
            candidate_count=1 + len(linked_changesets),
            redacted_candidates=1 + len(linked_changesets),
            required_grants=sorted(set(required_grants)),
        )

    return {
        "row": row,
        "workspace_visible": workspace_visible,
        "hidden_patterns": sorted(hidden_patterns),
        "visible_changesets": visible_changesets,
        "redacted_changesets": redacted_changesets,
        "access": {
            "allowed": True,
            "actor": actor,
            "action": "operation.read",
            "object": operation_id,
            "reason": "workspace" if workspace_visible else "linked_changeset",
            "origins": origins,
            "redacted_origins": redacted_origins,
        },
    }


def operation_event_ids(store: Store, operation_id: str, *, actor: str) -> tuple[list[str], int]:
    rows = store.rows(
        "SELECT * FROM events WHERE object_refs_json LIKE ? ORDER BY created_at, rowid",
        (f"%{operation_id}%",),
    )
    events = []
    redacted = 0
    for row in rows:
        try:
            events.append(event_view(store, row, actor=actor, record_denial=False)["id"])
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return events, redacted


def operation_data(store: Store, operation_id: str, *, actor: str) -> dict[str, Any]:
    context = operation_access_context(store, operation_id, actor=actor)
    operation = operation_detail_data(
        store,
        operation_id,
        context["hidden_patterns"],
        workspace_visible=context["workspace_visible"],
    )
    if operation is None:
        raise TriError("path_not_found", "Operation not found.", object=operation_id)
    source_events, redacted_events = operation_event_ids(store, operation_id, actor=actor)
    return {
        **operation,
        "changesets": context["visible_changesets"],
        "redacted_changesets": context["redacted_changesets"],
        "source_events": source_events,
        "redacted_source_events": redacted_events,
        "access": context["access"],
    }


def cmd_operations_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.operation.inspect", command, actor=args.actor, start=start, data=operation_data(store, args.operation, actor=args.actor))


def operation_ids_for_ref(store: Store, ref: str, *, actor: str) -> tuple[list[str], int]:
    if ref.startswith("ws_"):
        workspace = store.get_workspace(ref)
        require_object_access(store, workspace["id"], actor=actor, action="workspace.read")
        rows = store.rows("SELECT id FROM operations WHERE workspace_id = ? ORDER BY created_at, rowid", (workspace["id"],))
        return [row["id"] for row in rows], 0
    if ref.startswith("cs_"):
        changeset = store.get_changeset(ref)
        require_object_access(store, changeset["id"], actor=actor, action="changeset.read")
        return loads(changeset["operations_json"], []), 0
    raise TriError("path_not_found", "Expected a workspace or changeset id.", object=ref)


def cmd_operations_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    operation_ids, redacted = operation_ids_for_ref(store, args.ref, actor=args.actor)
    operations = []
    for operation_id in operation_ids:
        try:
            operations.append(operation_data(store, operation_id, actor=args.actor))
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return envelope("tri.operation.list", command, actor=args.actor, start=start, data={"ref": args.ref, "operations": operations, "redacted": redacted})


def changeset_data(store: Store, cs_id: str, *, include_hidden: bool = False, actor: str | None = None) -> dict[str, Any]:
    row = store.get_changeset(cs_id)
    stored_hidden_paths = loads(row["hidden_paths_json"], [])
    hidden_paths = effective_hidden_patterns(store, row["project_id"], stored_hidden_paths, actor=actor)
    touched_paths = loads(row["touched_paths_json"], [])
    visible_touched_paths = [path for path in touched_paths if path_visibility(path, hidden_paths)[0]]
    hidden_touched = len(touched_paths) - len(visible_touched_paths)
    lifecycle = loads(row["lifecycle_json"], {})
    if row["replacement_changeset_id"]:
        lifecycle = {**lifecycle, "superseded_by": row["replacement_changeset_id"]}
    workspace_id, redacted_workspace = workspace_ref_for_actor(store, row["workspace_id"], actor=actor)
    operation_ids = loads(row["operations_json"], [])
    operation_details = [
        operation
        for operation_id in operation_ids
        for operation in [operation_detail_data(store, operation_id, hidden_paths, include_hidden=include_hidden, workspace_visible=not redacted_workspace)]
        if operation is not None
    ]
    return {
        "id": row["id"],
        "project": row["project_id"],
        "workspace": workspace_id,
        "redacted_workspace": redacted_workspace,
        "title": row["title"],
        "status": row["status"],
        "base_snapshot": row["base_snapshot_id"],
        "head_snapshot": row["head_snapshot_id"],
        "operations": operation_ids,
        "operation_details": operation_details,
        "task_run": row["task_run_id"],
        "visibility": loads(row["visibility_json"], []),
        "hidden_paths": stored_hidden_paths,
        "effective_hidden_path_count": len(hidden_paths),
        "touched_paths": touched_paths if include_hidden else visible_touched_paths,
        "redacted_touched_paths": 0 if include_hidden else hidden_touched,
        "checks": loads(row["checks_json"], []),
        "reviews": loads(row["reviews_json"], []),
        "policy_decisions": loads(row["policy_decisions_json"], []),
        "export_status": row["export_status"],
        "advisory": row["advisory_id"],
        "replacement_changeset": row["replacement_changeset_id"],
        "lifecycle": lifecycle,
        "suggested_split_points": loads(row["suggested_split_points_json"], []),
        "stack": changeset_stack_data(store, row, actor=actor),
    }


def cmd_changeset_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_object_access(store, args.changeset, actor=args.actor, action="changeset.read")
    return envelope("tri.changeset.inspect", command, actor=args.actor, start=start, data=changeset_data(store, args.changeset, actor=args.actor))


CHANGESET_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"ready", "discarded"},
    "ready": {"reviewing", "superseded", "discarded"},
    "reviewing": {"changes_requested", "approved", "superseded"},
    "changes_requested": {"ready"},
    "approved": {"merged", "exported"},
}


def set_changeset_lifecycle(
    store: Store,
    cs: sqlite3.Row,
    *,
    actor: str,
    status: str,
    event_kind: str,
    reason: str | None = None,
    replacement: sqlite3.Row | None = None,
    reopen_reviews: bool = False,
) -> tuple[dict[str, Any], str]:
    previous_status = cs["status"]
    allowed = CHANGESET_TRANSITIONS.get(previous_status, set())
    if status not in allowed:
        raise TriError(
            "changeset_lifecycle_blocked",
            "Changeset lifecycle transition is not allowed.",
            object=cs["id"],
            status=previous_status,
            requested_status=status,
            allowed_transitions=sorted(allowed),
        )
    require_object_access(store, cs["id"], actor=actor, action="changeset.write")
    if replacement is not None:
        if replacement["id"] == cs["id"]:
            raise TriError("invalid_command", "A changeset cannot supersede itself.", object=cs["id"])
        if replacement["project_id"] != cs["project_id"]:
            raise TriError("invalid_command", "Replacement changeset must belong to the same project.", object=replacement["id"])
        if replacement["status"] in ("discarded", "superseded"):
            raise TriError("changeset_lifecycle_blocked", "Replacement changeset is not active.", object=replacement["id"], status=replacement["status"])
        require_object_access(store, replacement["id"], actor=actor, action="changeset.read")
    lifecycle = {
        **loads(cs["lifecycle_json"], {}),
        "previous_status": previous_status,
        "status": status,
        "transition": f"{previous_status}->{status}",
        "reason": reason,
        "actor": actor,
        "updated_at": utcnow(),
    }
    if replacement is not None:
        lifecycle["superseded_by"] = replacement["id"]
    review_ids = loads(cs["reviews_json"], [])
    reopened_reviews: list[str] = []
    with store.conn:
        store.conn.execute(
            "UPDATE changesets SET status = ?, replacement_changeset_id = ?, lifecycle_json = ? WHERE id = ?",
            (status, replacement["id"] if replacement is not None else cs["replacement_changeset_id"], dumps(lifecycle), cs["id"]),
        )
        if status in ("discarded", "superseded") and review_ids:
            store.conn.execute(
                f"UPDATE reviews SET status = ? WHERE id IN ({','.join('?' for _ in review_ids)}) AND status != ?",
                ("closed", *review_ids, "closed"),
            )
        if reopen_reviews and review_ids:
            review_rows = store.rows(
                f"SELECT id FROM reviews WHERE id IN ({','.join('?' for _ in review_ids)}) AND status = ?",
                (*review_ids, "changes_requested"),
            )
            reopened_reviews = [row["id"] for row in review_rows]
            if reopened_reviews:
                reopened_status = review_status_for_check_summary(changeset_check_summary(store, cs))
                store.conn.execute(
                    f"UPDATE reviews SET status = ? WHERE id IN ({','.join('?' for _ in reopened_reviews)})",
                    (reopened_status, *reopened_reviews),
                )
        evt = store.create_event(
            kind=event_kind,
            actor=actor,
            project_id=cs["project_id"],
            workspace_id=cs["workspace_id"],
            object_refs={
                "changeset": cs["id"],
                "previous_status": previous_status,
                "status": status,
                "replacement": replacement["id"] if replacement is not None else None,
                "closed_reviews": review_ids if status in ("discarded", "superseded") else [],
                "reopened_reviews": reopened_reviews,
            },
        )
    data = changeset_data(store, cs["id"], actor=actor)
    data["previous_status"] = previous_status
    data["transition"] = f"{previous_status}->{status}"
    return data, evt


def cmd_changeset_ready(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    data, evt = set_changeset_lifecycle(store, cs, actor=args.actor, status="ready", event_kind="changeset.ready", reason=args.reason, reopen_reviews=True)
    return envelope("tri.changeset.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_changeset_discard(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    data, evt = set_changeset_lifecycle(store, cs, actor=args.actor, status="discarded", event_kind="changeset.discarded", reason=args.reason)
    return envelope("tri.changeset.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_changeset_supersede(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    replacement = store.get_changeset(args.replacement)
    data, evt = set_changeset_lifecycle(store, cs, actor=args.actor, status="superseded", event_kind="changeset.superseded", reason=args.reason, replacement=replacement)
    return envelope("tri.changeset.lifecycle", command, actor=args.actor, start=start, events=[evt], data=data)


def changeset_operation_entries(store: Store, cs: sqlite3.Row) -> list[dict[str, Any]]:
    entries = []
    for index, op_id in enumerate(loads(cs["operations_json"], []), start=1):
        row = store.row("SELECT id, path_set_json FROM operations WHERE id = ?", (op_id,))
        if row is None:
            raise TriError("path_not_found", "Changeset operation not found.", object=op_id)
        entries.append({"index": index, "id": row["id"], "paths": loads(row["path_set_json"], [])})
    return entries


def parse_operation_selection(selector: str, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = [item.strip() for item in selector.split(",") if item.strip()]
    if not tokens:
        raise TriError("invalid_command", "--operations requires an operation id, 1-based index, or inclusive range.")
    index_by_id = {op["id"]: op["index"] for op in operations}
    selected: set[int] = set()
    max_index = len(operations)
    for token in tokens:
        if token in index_by_id:
            selected.add(index_by_id[token])
            continue
        delimiter = ".." if ".." in token else "-" if token.count("-") == 1 else None
        if delimiter:
            start_text, end_text = [part.strip() for part in token.split(delimiter, 1)]
            if not start_text.isdigit() or not end_text.isdigit():
                raise TriError("invalid_command", "Operation ranges must use 1-based numeric indexes.", selector=token)
            start_index = int(start_text)
            end_index = int(end_text)
            if start_index < 1 or end_index > max_index or start_index > end_index:
                raise TriError("invalid_command", "Operation range is outside the changeset operation list.", selector=token, max_index=max_index)
            selected.update(range(start_index, end_index + 1))
            continue
        if token.isdigit():
            index = int(token)
            if index < 1 or index > max_index:
                raise TriError("invalid_command", "Operation index is outside the changeset operation list.", selector=token, max_index=max_index)
            selected.add(index)
            continue
        raise TriError("invalid_command", "Unknown operation selector.", selector=token)
    return [op for op in operations if op["index"] in selected]


def split_preview_payload(
    *,
    changeset: str,
    selector: str,
    selected_ops: list[dict[str, Any]],
    hidden_patterns: list[str],
    patterns: list[str] | None = None,
) -> dict[str, Any]:
    visible_paths: set[str] = set()
    redacted_paths: set[str] = set()
    operation_payload = []
    for op in selected_ops:
        op_visible_paths = []
        for path in op["paths"]:
            visible, reason = path_visibility(path, hidden_patterns)
            if visible:
                visible_paths.add(path)
                op_visible_paths.append(path)
            else:
                redacted_paths.add(path)
        operation_payload.append({"index": op["index"], "id": op["id"], "paths": sorted(op_visible_paths)})
    data = {
        "changeset": changeset,
        "mode": "preview",
        "selector": selector,
        "operations": operation_payload,
        "paths": sorted(visible_paths),
        "redacted_paths": len(redacted_paths),
    }
    if patterns is not None:
        data["patterns"] = patterns
    return data


def split_head_from_operations(store: Store, base_snapshot: str, selected_ops: list[dict[str, Any]]) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    entries = dict(store.load_snapshot_tree(base_snapshot))
    touched: set[str] = set()
    for op in selected_ops:
        row = store.row("SELECT input_snapshot_id, output_snapshot_id, path_set_json FROM operations WHERE id = ?", (op["id"],))
        if row is None:
            raise TriError("path_not_found", "Changeset operation not found.", object=op["id"])
        before = store.load_snapshot_tree(row["input_snapshot_id"])
        after = store.load_snapshot_tree(row["output_snapshot_id"])
        for path in loads(row["path_set_json"], []):
            if before.get(path) == after.get(path):
                continue
            touched.add(path)
            if path in after:
                entries[path] = after[path]
            else:
                entries.pop(path, None)
    return base_snapshot, dict(sorted(entries.items())), sorted(touched)


def apply_changeset_split(
    store: Store,
    source: sqlite3.Row,
    *,
    actor: str,
    selector: str,
    selected_ops: list[dict[str, Any]],
    hidden_patterns: list[str],
    title: str | None,
) -> tuple[dict[str, Any], str]:
    if not selected_ops:
        raise TriError("invalid_command", "Applied changeset split requires at least one selected operation.", object=source["id"])
    _, split_entries, _ = split_head_from_operations(store, source["base_snapshot_id"], selected_ops)
    base_entries = store.load_snapshot_tree(source["base_snapshot_id"])
    final_touched = changed_paths(base_entries, split_entries)
    if not final_touched:
        raise TriError("invalid_command", "Applied changeset split would create an empty changeset.", object=source["id"])
    split_id = new_id("cs")
    now = utcnow()
    operation_ids = [op["id"] for op in selected_ops]
    with store.conn:
        snapshot = store.create_snapshot(
            source["project_id"],
            split_entries,
            parents=[source["base_snapshot_id"]],
            source="changeset_split",
            actor=actor,
            metadata={"source_changeset": source["id"], "operations": operation_ids, "selector": selector},
        )
        lifecycle = {
            "created_by": actor,
            "created_at": now,
            "split_from": source["id"],
            "source_operations": operation_ids,
            "selector": selector,
        }
        store.conn.execute(
            """
            INSERT INTO changesets (
              id, project_id, workspace_id, title, base_snapshot_id, head_snapshot_id, status,
              operations_json, task_run_id, visibility_json, hidden_paths_json, touched_paths_json,
              checks_json, reviews_json, policy_decisions_json, export_status,
              stack_parent_id, advisory_id, replacement_changeset_id, lifecycle_json,
              suggested_split_points_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                split_id,
                source["project_id"],
                source["workspace_id"],
                title or f"Split from {source['title']}",
                source["base_snapshot_id"],
                snapshot["id"],
                "ready",
                dumps(operation_ids),
                source["task_run_id"],
                source["visibility_json"],
                source["hidden_paths_json"],
                dumps(final_touched),
                dumps([]),
                dumps([]),
                dumps([]),
                "not_exported",
                None,
                source["advisory_id"],
                None,
                dumps(lifecycle),
                dumps([]),
                now,
            ),
        )
        evt = store.create_event(
            kind="changeset.split_created",
            actor=actor,
            project_id=source["project_id"],
            workspace_id=source["workspace_id"],
            object_refs={"source_changeset": source["id"], "changeset": split_id, "operations": operation_ids, "paths": final_touched},
            input_fingerprints={"source_head_snapshot": source["head_snapshot_id"], "base_snapshot": source["base_snapshot_id"]},
            output_fingerprints={"head_snapshot": snapshot["id"]},
        )
    data = changeset_data(store, split_id, actor=actor)
    return data, evt


def cmd_changeset_split(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = store.get_changeset(args.changeset)
    require_object_access(store, row["id"], actor=args.actor, action="changeset.write" if args.apply else "changeset.read")
    hidden_patterns = effective_hidden_patterns(store, row["project_id"], loads(row["hidden_paths_json"], []), actor=args.actor)
    operations = changeset_operation_entries(store, row)
    if args.paths:
        patterns = [item.strip() for item in args.paths.split(",") if item.strip()]
        if not patterns:
            raise TriError("invalid_command", "--paths requires at least one glob pattern.")
        selected_ops = []
        for op in operations:
            visible_paths = [path for path in op["paths"] if path_visibility(path, hidden_patterns)[0]]
            if any(any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in visible_paths):
                selected_ops.append(op)
        data = split_preview_payload(changeset=args.changeset, selector="paths", selected_ops=selected_ops, hidden_patterns=hidden_patterns, patterns=patterns)
    else:
        selected_ops = parse_operation_selection(args.operations, operations)
        data = split_preview_payload(changeset=args.changeset, selector="operations", selected_ops=selected_ops, hidden_patterns=hidden_patterns)
        data["operation_selector"] = args.operations
    if args.apply:
        split_changeset, evt = apply_changeset_split(
            store,
            row,
            actor=args.actor,
            selector=data["selector"],
            selected_ops=selected_ops,
            hidden_patterns=hidden_patterns,
            title=args.title,
        )
        data = {
            **data,
            "mode": "applied",
            "split_changeset": split_changeset["id"],
            "base_snapshot": split_changeset["base_snapshot"],
            "head_snapshot": split_changeset["head_snapshot"],
            "created": split_changeset,
        }
        return envelope("tri.changeset.split", command, actor=args.actor, start=start, events=[evt], data=data)
    return envelope("tri.changeset.split", command, actor=args.actor, start=start, data=data)


def stack_would_cycle(store: Store, changeset_id: str, parent_id: str) -> bool:
    current = parent_id
    seen: set[str] = set()
    while current:
        if current == changeset_id:
            return True
        if current in seen:
            return True
        seen.add(current)
        row = store.row("SELECT stack_parent_id FROM changesets WHERE id = ?", (current,))
        current = row["stack_parent_id"] if row else None
    return False


def cmd_changeset_stack(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    child = store.get_changeset(args.changeset)
    parent = store.get_changeset(args.after)
    require_object_access(store, child["id"], actor=args.actor, action="changeset.write")
    require_object_access(store, parent["id"], actor=args.actor, action="changeset.read")
    if child["id"] == parent["id"]:
        raise TriError("invalid_command", "A changeset cannot depend on itself.", object=child["id"])
    if child["project_id"] != parent["project_id"]:
        raise TriError("invalid_command", "Stacked changesets must belong to the same project.", object=child["id"], parent=parent["id"])
    if stack_would_cycle(store, child["id"], parent["id"]):
        raise TriError("invalid_command", "Changeset stack would create a dependency cycle.", object=child["id"], parent=parent["id"])
    with store.conn:
        store.conn.execute("UPDATE changesets SET stack_parent_id = ? WHERE id = ?", (parent["id"], child["id"]))
        evt = store.create_event(
            kind="changeset.stacked",
            actor=args.actor,
            project_id=child["project_id"],
            workspace_id=child["workspace_id"],
            object_refs={"changeset": child["id"], "parent": parent["id"]},
            input_fingerprints={"child_base_snapshot": child["base_snapshot_id"], "parent_head_snapshot": parent["head_snapshot_id"]},
        )
    data = changeset_data(store, child["id"], actor=args.actor)
    return envelope("tri.changeset.stack", command, actor=args.actor, start=start, events=[evt], data={"changeset": child["id"], "stack": data["stack"]})


def latest_check_commands_for_changeset(store: Store, cs: sqlite3.Row) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for check_id in loads(cs["checks_json"], []):
        check = check_data(store, check_id)
        latest[dumps(check["command"])] = {
            "command": check["command"],
            "rerun_of": check["id"],
            "assumptions": check["assumptions"],
            "source": {"type": "check", "id": check["id"]},
        }
    return list(latest.values())


def run_check_for_changeset_head(
    store: Store,
    cs: sqlite3.Row,
    cmd: list[str],
    *,
    actor: str,
    command: list[str],
    start: float,
    rerun_of: str | None = None,
    assumptions: list[str] | None = None,
    snapshot: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run a check against the changeset head, or against `snapshot` (post-merge verification)."""
    check_id = new_id("check")
    run_lease = RunLease(store, kind="revalidation", key=check_id, workspace_id=cs["workspace_id"], before_snapshot=snapshot or cs["head_snapshot_id"])
    try:
        return _run_check_for_changeset_head(store, cs, cmd, check_id=check_id, run_lease=run_lease, actor=actor, command=command, start=start, rerun_of=rerun_of, assumptions=assumptions, snapshot=snapshot)
    finally:
        run_lease.release()


def _run_check_for_changeset_head(
    store: Store,
    cs: sqlite3.Row,
    cmd: list[str],
    *,
    check_id: str,
    run_lease: RunLease,
    actor: str,
    command: list[str],
    start: float,
    rerun_of: str | None = None,
    assumptions: list[str] | None = None,
    snapshot: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if not cmd:
        raise TriError("invalid_command", "Changeset revalidation command cannot be empty.", object=cs["id"])
    before = snapshot or cs["head_snapshot_id"]
    before_entries = store.load_snapshot_tree(before)
    normalized_assumptions = normalize_assumptions(assumptions)
    work = store.runs / check_id / "work"
    materialization = materialize_for_run(store, before_entries, run_lease, base_snapshot=snapshot or cs["base_snapshot_id"])
    run_lease.record_materialization(materialization)
    with store.conn:
        started_evt = store.create_event(
            kind="check.revalidation_started",
            actor=actor,
            project_id=cs["project_id"],
            workspace_id=cs["workspace_id"],
            object_refs={"check": check_id, "changeset": cs["id"], "runner": "local_changeset", "command": cmd, "rerun_of": rerun_of, "assumptions": normalized_assumptions},
            input_fingerprints={"changeset_head": before},
        )
    started = time.time()
    run_cwd = run_lease.dir if isinstance(materialization, OverlayMaterialization) else work
    box = command_sandbox(store, run_lease.dir, purpose="changeset_revalidate")
    proc = sandbox.run(box, materialization.wrap(cmd), cwd=run_cwd, env=sandbox_env(box, run_lease.dir, os.environ, {}))
    duration_ms = int((time.time() - started) * 1000)
    rules = import_rules(store, cs["project_id"], before_entries)
    scanned_changed, scanned_deleted = materialization.scan(before_entries, ignore=rules)
    after_entries = entries_after_scan(store, materialization.content_root, before_entries, scanned_changed, scanned_deleted)
    paths = changed_paths(before_entries, after_entries)
    cost = local_check_cost(cmd, duration_ms, proc.stdout, proc.stderr)
    stdout_artifact = write_artifact(store, "stdout", f"{check_id}-stdout.txt", proc.stdout, labels=[])
    stderr_artifact = write_artifact(store, "stderr", f"{check_id}-stderr.txt", proc.stderr, labels=[])
    status = "passed" if proc.returncode == 0 else "failed"
    materialized_directory = materialized_directory_record(
        path=work,
        input_snapshot=before,
        output_snapshot=before,
        before_entries=before_entries,
        after_entries=after_entries,
        mode="ephemeral_changeset_revalidation",
        materialization=materialization,
    )
    environment_policy = check_environment_policy([], {})
    with store.conn:
        store.conn.execute(
            """
            INSERT INTO check_runs (
              id, workspace_id, changeset_id, task_run_id, rerun_of, actor, status, runner, command_json,
              snapshot_before, snapshot_after, exit_status, duration_ms, logs_json,
              artifacts_json, secret_leases_json, materialized_directory_json, environment_policy_json,
              file_changes_json, cost_json, assumptions_json, reproducibility, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check_id,
                cs["workspace_id"],
                cs["id"],
                cs["task_run_id"],
                rerun_of,
                actor,
                status,
                "post_merge" if snapshot else "local_changeset",
                dumps(cmd),
                before,
                before,
                proc.returncode,
                duration_ms,
                dumps({"stdout": stdout_artifact, "stderr": stderr_artifact}),
                dumps([stdout_artifact, stderr_artifact]),
                dumps([]),
                dumps(materialized_directory),
                dumps(environment_policy),
                dumps(paths),
                dumps(cost),
                dumps(normalized_assumptions),
                "post_merge_replayable" if snapshot else "changeset_head_replayable",
                utcnow(),
            ),
        )
        attach_assumptions_to_task_run(store, cs["task_run_id"], normalized_assumptions)
        evt = store.create_event(
            kind="check.revalidated",
            actor=actor,
            project_id=cs["project_id"],
            workspace_id=cs["workspace_id"],
            object_refs={"check": check_id, "changeset": cs["id"], "status": status, "rerun_of": rerun_of, "file_changes": paths, "assumptions": normalized_assumptions},
            input_fingerprints={"changeset_head": before},
            output_fingerprints={"changeset_head": before},
        )
    return check_data(store, check_id), [started_evt, evt]


def cmd_changeset_revalidate(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    require_object_access(store, cs["id"], actor=args.actor, action="changeset.write")
    selected: list[dict[str, Any]] = []
    if args.check:
        known_checks = set(loads(cs["checks_json"], []))
        for check_id in args.check:
            if check_id not in known_checks:
                raise TriError("invalid_command", "Revalidation check must already be attached to the changeset.", object=check_id, changeset=cs["id"])
            check = check_data(store, check_id)
            selected.append({"command": check["command"], "rerun_of": check["id"], "assumptions": check["assumptions"], "source": {"type": "check", "id": check["id"]}})
    elif args.required:
        selected = [{"command": spec["command"], "rerun_of": None, "source": spec.get("source", {"type": "task_required_check"})} for spec in task_required_checks_for_changeset(store, cs)]
    else:
        selected = latest_check_commands_for_changeset(store, cs)
        if not selected:
            selected = [{"command": spec["command"], "rerun_of": None, "source": spec.get("source", {"type": "task_required_check"})} for spec in task_required_checks_for_changeset(store, cs)]
    if not selected:
        raise TriError("invalid_command", "No checks are available to revalidate for this changeset.", object=cs["id"])

    events: list[str] = []
    checks: list[dict[str, Any]] = []
    for spec in selected:
        check, check_events = run_check_for_changeset_head(store, cs, spec["command"], actor=args.actor, command=command, start=start, rerun_of=spec["rerun_of"], assumptions=spec.get("assumptions", []))
        check["source"] = spec["source"]
        checks.append(check)
        events.extend(check_events)
    with store.conn:
        check_ids = loads(cs["checks_json"], [])
        check_ids.extend(check["id"] for check in checks)
        store.conn.execute("UPDATE changesets SET checks_json = ? WHERE id = ?", (dumps(check_ids), cs["id"]))
        evt = store.create_event(
            kind="changeset.revalidated",
            actor=args.actor,
            project_id=cs["project_id"],
            workspace_id=cs["workspace_id"],
            object_refs={"changeset": cs["id"], "checks": [check["id"] for check in checks]},
            input_fingerprints={"head_snapshot": cs["head_snapshot_id"]},
            output_fingerprints={"head_snapshot": cs["head_snapshot_id"]},
        )
        events.append(evt)
    data = changeset_data(store, cs["id"], actor=args.actor)
    return envelope(
        "tri.changeset.revalidate",
        command,
        actor=args.actor,
        start=start,
        events=events,
        data={"changeset": cs["id"], "head_snapshot": cs["head_snapshot_id"], "checks": checks, "stack": data["stack"]},
    )


def cmd_changeset_merge(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    # Concurrent merges race on one compare-and-swap of the target row. A loser
    # recomputes against the new target instead of failing back to the agent.
    retries = max(0, getattr(args, "retry", 0) or 0)
    attempt = 0
    while True:
        try:
            output = changeset_merge_once(store, args, command, start)
        except TriError as exc:
            if exc.diagnostic["code"] != "stale_target_snapshot" or attempt >= retries:
                raise
            attempt += 1
            time.sleep(min(0.05 * attempt, 0.5))
            continue
        output["data"]["merge_attempts"] = attempt + 1
        return output


def changeset_merge_once(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    target = resolve_target_row(store, cs["project_id"], args.target)
    decision, diagnostics = evaluate_export_policy(store, cs, actor=args.actor, action="changeset.merge")
    policy_event = store.record_policy_decision(actor=args.actor, action="changeset.merge", object_id=cs["id"], decision=decision, project_id=cs["project_id"])
    if diagnostics:
        return envelope(
            "tri.changeset.merge",
            command,
            actor=args.actor,
            start=start,
            ok=False,
            events=[policy_event],
            diagnostics=diagnostics,
            data={
                "changeset": cs["id"],
                "target": target["name"],
                "status": "policy_blocked",
                "previous_snapshot": target["snapshot_id"],
                "snapshot": target["snapshot_id"],
                "changed_paths": [],
                "conflicts": [],
            },
        )

    previous_snapshot = target["snapshot_id"]
    base_snapshot = cs["base_snapshot_id"]
    head_snapshot = cs["head_snapshot_id"]
    text_merged: list[str] = []
    if previous_snapshot == head_snapshot:
        with store.conn:
            store.conn.execute("UPDATE changesets SET status = ? WHERE id = ?", ("merged", cs["id"]))
            if cs["workspace_id"]:
                store.conn.execute("UPDATE workspaces SET status = ?, updated_at = ? WHERE id = ? AND status = ?", ("promoted", utcnow(), cs["workspace_id"], "active"))
            evt = store.create_event(
                kind="changeset.merged",
                actor=args.actor,
                project_id=cs["project_id"],
                workspace_id=cs["workspace_id"],
                object_refs={"changeset": cs["id"], "target": target["id"], "target_name": target["name"], "snapshot": head_snapshot, "mode": "already_current"},
                policy_result=decision,
            )
        return envelope(
            "tri.changeset.merge",
            command,
            actor=args.actor,
            start=start,
            events=[policy_event, evt],
            data={"changeset": cs["id"], "target": target["name"], "status": "merged", "mode": "already_current", "previous_snapshot": previous_snapshot, "snapshot": head_snapshot, "changed_paths": [], "conflicts": []},
        )

    if previous_snapshot == base_snapshot:
        merged_snapshot = head_snapshot
        changed = changed_paths(store.load_snapshot_tree(previous_snapshot), store.load_snapshot_tree(merged_snapshot))
        mode = "fast_forward"
    else:
        base_entries = store.load_snapshot_tree(base_snapshot)
        head_entries = store.load_snapshot_tree(head_snapshot)
        target_entries = store.load_snapshot_tree(previous_snapshot)
        merged_entries, conflicts, _ = merge_entries(store, base_entries, head_entries, target_entries, text_merged)
        if conflicts:
            diagnostic = TriError(
                "conflict_required",
                "Changeset merge requires conflict resolution.",
                object=cs["id"],
                target=target["name"],
                conflicts=conflicts,
            ).diagnostic
            with store.conn:
                evt = store.create_event(
                    kind="changeset.merge_conflict",
                    actor=args.actor,
                    project_id=cs["project_id"],
                    workspace_id=cs["workspace_id"],
                    object_refs={"changeset": cs["id"], "target": target["id"], "target_name": target["name"], "conflicts": conflicts},
                    input_fingerprints={"base_snapshot": base_snapshot, "head_snapshot": head_snapshot, "target_snapshot": previous_snapshot},
                    policy_result=decision,
                )
            return envelope(
                "tri.changeset.merge",
                command,
                actor=args.actor,
                start=start,
                ok=False,
                events=[policy_event, evt],
                diagnostics=[diagnostic],
                data={"changeset": cs["id"], "target": target["name"], "status": "conflict_required", "previous_snapshot": previous_snapshot, "snapshot": previous_snapshot, "changed_paths": [], "conflicts": conflicts},
            )
        changed = changed_paths(target_entries, merged_entries)
        with store.conn:
            snapshot = store.create_snapshot(
                cs["project_id"],
                merged_entries,
                parents=[previous_snapshot, head_snapshot],
                source="merge",
                actor=args.actor,
                metadata={"changeset": cs["id"], "target": target["name"], "operation": "changeset_merge", "text_merged_paths": text_merged},
            )
        merged_snapshot = snapshot["id"]
        mode = "merge"

    with store.conn:
        updated = store.conn.execute(
            "UPDATE targets SET snapshot_id = ? WHERE id = ? AND snapshot_id = ?",
            (merged_snapshot, target["id"], previous_snapshot),
        )
        if updated.rowcount != 1:
            raise TriError("stale_target_snapshot", "Target changed while merging changeset.", object=target["name"], expected=previous_snapshot, retryable=True)
        store.conn.execute("UPDATE changesets SET status = ? WHERE id = ?", ("merged", cs["id"]))
        if cs["workspace_id"]:
            store.conn.execute("UPDATE workspaces SET status = ?, updated_at = ? WHERE id = ? AND status = ?", ("promoted", utcnow(), cs["workspace_id"], "active"))
            ws = store.get_workspace(cs["workspace_id"])
            if ws["task_id"]:
                store.conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND status IN (?, ?)", ("accepted", ws["task_id"], "running", "reviewing"))
        evt = store.create_event(
            kind="changeset.merged",
            actor=args.actor,
            project_id=cs["project_id"],
            workspace_id=cs["workspace_id"],
            object_refs={"changeset": cs["id"], "target": target["id"], "target_name": target["name"], "snapshot": merged_snapshot, "previous_snapshot": previous_snapshot, "paths": changed, "text_merged_paths": text_merged, "mode": mode},
            input_fingerprints={"target_snapshot": previous_snapshot, "changeset_head": head_snapshot},
            output_fingerprints={"target_snapshot": merged_snapshot},
            policy_result=decision,
        )
        verification = queue_post_merge_verification(store, cs, decision, target_id=target["id"], merged_snapshot=merged_snapshot, pre_merge_snapshot=previous_snapshot, actor=args.actor)
    return envelope(
        "tri.changeset.merge",
        command,
        actor=args.actor,
        start=start,
        events=[policy_event, evt],
        data={"changeset": cs["id"], "target": target["name"], "status": "merged", "mode": mode, "previous_snapshot": previous_snapshot, "snapshot": merged_snapshot, "changed_paths": changed, "text_merged_paths": text_merged, "conflicts": [], "merge_rule": decision["review"]["merge_rule"], "verification": verification},
    )


def changeset_check_summary(store: Store, cs: sqlite3.Row) -> dict[str, Any]:
    checks = [check_data(store, check_id) for check_id in loads(cs["checks_json"], [])]
    required_diagnostics, latest_required, _ = required_check_diagnostics(store, cs)
    return {
        "total": len(checks),
        "passed": sum(1 for check in checks if check["status"] == "passed"),
        "failed": sum(1 for check in checks if check["status"] == "failed"),
        "latest_required": [check["id"] for check in latest_required],
        "required_satisfied": not required_diagnostics,
        "required_diagnostics": required_diagnostics,
    }


def review_status_for_check_summary(summary: dict[str, Any]) -> str:
    return "open" if summary.get("required_satisfied") else "waiting_for_checks"


def cmd_review_create(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    if cs["status"] not in ("draft", "ready"):
        raise TriError(
            "changeset_lifecycle_blocked",
            "Changeset cannot accept a new review in its current state.",
            object=cs["id"],
            status=cs["status"],
            allowed_statuses=["draft", "ready"],
        )
    require_object_access(store, cs["id"], actor=args.actor, action="changeset.write")
    check_summary = changeset_check_summary(store, cs)
    review_status = review_status_for_check_summary(check_summary)
    diff = diff_data(store, cs["base_snapshot_id"], cs["head_snapshot_id"], effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=args.actor))
    review_id = new_id("review")
    with store.conn:
        store.conn.execute(
            "INSERT INTO reviews (id, changeset_id, status, reviewers_json, check_summary_json, risk_summary, export_preview_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, cs["id"], review_status, dumps(args.reviewers or []), dumps(check_summary), "unreviewed", dumps(diff["summary"]), utcnow()),
        )
        reviews = loads(cs["reviews_json"], [])
        reviews.append(review_id)
        store.conn.execute("UPDATE changesets SET reviews_json = ?, status = ? WHERE id = ?", (dumps(reviews), "reviewing", cs["id"]))
        evt = store.create_event(
            kind="review.created",
            actor=args.actor,
            project_id=cs["project_id"],
            object_refs={"review": review_id, "changeset": cs["id"]},
        )
    return envelope(
        "tri.review.create",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={
            "id": review_id,
            "changeset": cs["id"],
            "status": review_status,
            "reviewers": args.reviewers or [],
            "check_summary": check_summary,
            "export_preview": diff["summary"],
        },
    )


def review_action_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "review": row["review_id"],
        "actor": row["actor"],
        "kind": row["kind"],
        "body": row["body"],
        "metadata": loads(row["metadata_json"], {}),
        "created_at": row["created_at"],
    }


def redacted_review_action_data(action: dict[str, Any]) -> dict[str, Any]:
    metadata = action.get("metadata", {})
    redacted_metadata: dict[str, Any] = {}
    if action["kind"] == "commented":
        for key in ("thread", "reply_to"):
            if key in metadata:
                redacted_metadata[key] = metadata[key]
        if "path" in metadata or "line" in metadata:
            redacted_metadata["anchor_redacted"] = True
    elif metadata:
        redacted_metadata["redacted"] = True
    return {**action, "metadata": redacted_metadata}


def review_assignment_state(review: sqlite3.Row, actions: list[dict[str, Any]], *, extra_approval_actor: str | None = None) -> dict[str, Any]:
    reviewers = loads(review["reviewers_json"], [])
    approvals = [action["actor"] for action in actions if action["kind"] == "approved"]
    if extra_approval_actor:
        approvals.append(extra_approval_actor)
    unique_approvals = sorted(set(approvals))
    maintainer_override = any("role:maintainer" in actor_selectors(actor) for actor in approvals)
    approved_reviewers = sorted(actor for actor in reviewers if actor in unique_approvals)
    pending = [] if maintainer_override else [actor for actor in reviewers if actor not in unique_approvals]
    return {
        "reviewers": reviewers,
        "approvals": unique_approvals,
        "approved_reviewers": approved_reviewers,
        "pending_reviewers": pending,
        "maintainer_override": maintainer_override,
        "complete": not pending,
    }


def get_review_action(store: Store, action_id: str) -> sqlite3.Row:
    row = store.row("SELECT * FROM review_actions WHERE id = ?", (action_id,))
    if row is None:
        raise TriError("path_not_found", "Review action not found.", object=action_id)
    return row


def review_action_view(store: Store, row: sqlite3.Row, *, actor: str) -> dict[str, Any]:
    review = store.get_review(row["review_id"])
    require_object_access(store, review["id"], actor=actor, action="review.read")
    action = review_action_data(row)
    decision, _ = object_access_decision(store, review["changeset_id"], actor=actor, action="changeset.read")
    if not decision["allowed"]:
        action = redacted_review_action_data(action)
    return action


def require_review_open(review: sqlite3.Row) -> None:
    if review["status"] == "closed":
        raise TriError("review_closed", "Review is closed and cannot be changed.", object=review["id"])


def comment_thread_id(action: dict[str, Any]) -> str:
    return action["metadata"].get("thread") or action["id"]


def review_comment_threads(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    threads: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for action in actions:
        if action["kind"] != "commented":
            continue
        metadata = action["metadata"]
        thread_id = comment_thread_id(action)
        if thread_id not in threads:
            order.append(thread_id)
            threads[thread_id] = {
                "id": thread_id,
                "status": "open",
                "path": metadata.get("path"),
                "line": metadata.get("line"),
                "comments": [],
                "resolved_by": None,
                "resolved_at": None,
            }
        threads[thread_id]["comments"].append(
            {
                "id": action["id"],
                "actor": action["actor"],
                "body": action["body"],
                "reply_to": metadata.get("reply_to"),
                "created_at": action["created_at"],
            }
        )
    for action in actions:
        if action["kind"] != "thread_resolved":
            continue
        thread_id = action["metadata"].get("thread")
        if thread_id in threads:
            threads[thread_id]["status"] = "resolved"
            threads[thread_id]["resolved_by"] = action["actor"]
            threads[thread_id]["resolved_at"] = action["created_at"]
    return [threads[thread_id] for thread_id in order]


def record_review_action(
    store: Store,
    review: sqlite3.Row,
    *,
    actor: str,
    kind: str,
    body: str | None = None,
    metadata: dict[str, Any] | None = None,
    review_status: str | None = None,
    changeset_status: str | None = None,
    risk_summary: str | None = None,
    check_summary: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    cs = store.get_changeset(review["changeset_id"])
    action_id = new_id("revact")
    with store.conn:
        store.conn.execute(
            "INSERT INTO review_actions (id, review_id, actor, kind, body, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action_id, review["id"], actor, kind, body, dumps(metadata or {}), utcnow()),
        )
        if review_status:
            store.conn.execute("UPDATE reviews SET status = ? WHERE id = ?", (review_status, review["id"]))
        if risk_summary:
            store.conn.execute("UPDATE reviews SET risk_summary = ? WHERE id = ?", (risk_summary, review["id"]))
        if check_summary is not None:
            store.conn.execute("UPDATE reviews SET check_summary_json = ? WHERE id = ?", (dumps(check_summary), review["id"]))
        if changeset_status:
            store.conn.execute("UPDATE changesets SET status = ? WHERE id = ?", (changeset_status, cs["id"]))
        evt = store.create_event(
            kind=f"review.{kind}",
            actor=actor,
            project_id=cs["project_id"],
            object_refs={"review": review["id"], "changeset": cs["id"], "review_action": action_id, **(metadata or {})},
        )
    action = review_action_data(store.row("SELECT * FROM review_actions WHERE id = ?", (action_id,)))
    return action, evt


def cmd_review_comment(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    require_object_access(store, review["changeset_id"], actor=args.actor, action="changeset.write")
    metadata: dict[str, Any] = {}
    if args.path:
        metadata["path"] = normalize_path(args.path)
    if args.line is not None:
        if args.line < 1:
            raise TriError("invalid_command", "Review comment line must be positive.", line=args.line)
        metadata["line"] = args.line
    if args.reply_to:
        parent = get_review_action(store, args.reply_to)
        if parent["review_id"] != review["id"] or parent["kind"] != "commented":
            raise TriError("invalid_command", "Replies must target a comment on the same review.", object=args.reply_to)
        parent_action = review_action_data(parent)
        metadata["reply_to"] = parent_action["id"]
        metadata["thread"] = comment_thread_id(parent_action)
    action, evt = record_review_action(store, review, actor=args.actor, kind="commented", body=args.body, metadata=metadata)
    return envelope("tri.review.action", command, actor=args.actor, start=start, events=[evt], data={"review": args.review, "action": action, "status": store.get_review(args.review)["status"]})


def cmd_review_resolve_thread(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    require_object_access(store, review["changeset_id"], actor=args.actor, action="changeset.write")
    root = get_review_action(store, args.thread)
    if root["review_id"] != review["id"] or root["kind"] != "commented":
        raise TriError("invalid_command", "Thread resolution must target a comment on the same review.", object=args.thread)
    root_action = review_action_data(root)
    thread_id = comment_thread_id(root_action)
    action, evt = record_review_action(store, review, actor=args.actor, kind="thread_resolved", body=args.body, metadata={"thread": thread_id})
    return envelope("tri.review.action", command, actor=args.actor, start=start, events=[evt], data={"review": args.review, "action": action, "thread": thread_id, "status": store.get_review(args.review)["status"]})


def cmd_review_request_changes(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    require_object_access(store, review["changeset_id"], actor=args.actor, action="changeset.write")
    action, evt = record_review_action(
        store,
        review,
        actor=args.actor,
        kind="changes_requested",
        body=args.body,
        review_status="changes_requested",
        changeset_status="changes_requested",
    )
    return envelope("tri.review.action", command, actor=args.actor, start=start, events=[evt], data={"review": args.review, "action": action, "status": "changes_requested"})


def cmd_review_approve(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    cs = store.get_changeset(review["changeset_id"])
    require_object_access(store, cs["id"], actor=args.actor, action="changeset.write")
    if cs["status"] == "changes_requested":
        raise TriError(
            "changeset_lifecycle_blocked",
            "Changeset must be marked ready before approval after requested changes.",
            object=cs["id"],
            status=cs["status"],
            required_transition="changeset.ready",
        )
    reviewers = loads(review["reviewers_json"], [])
    if reviewers and args.actor not in reviewers and "role:maintainer" not in actor_selectors(args.actor):
        raise TriError(
            "reviewer_not_assigned",
            "Actor is not assigned as a reviewer for this review.",
            object=review["id"],
            actor=args.actor,
            reviewers=reviewers,
        )
    check_diagnostics, _, _ = required_check_diagnostics(store, cs)
    if check_diagnostics:
        first = check_diagnostics[0]
        raise TriError(
            "missing_required_check",
            "A review cannot be approved while required checks are missing, failing, or stale.",
            object=cs["id"],
            diagnostics=check_diagnostics,
            required_checks=first.get("required_checks", []),
            checks=first.get("checks", []),
        )
    existing_actions = [review_action_data(action) for action in store.rows("SELECT * FROM review_actions WHERE review_id = ? ORDER BY created_at, rowid", (review["id"],))]
    assignment = review_assignment_state(review, existing_actions, extra_approval_actor=args.actor)
    review_status = "approved" if assignment["complete"] else "waiting_for_reviewers"
    changeset_status = "approved" if assignment["complete"] else "reviewing"
    action, evt = record_review_action(
        store,
        review,
        actor=args.actor,
        kind="approved",
        body=args.body,
        metadata={"pending_reviewers": assignment["pending_reviewers"], "completes_review": assignment["complete"]},
        review_status=review_status,
        changeset_status=changeset_status,
    )
    return envelope("tri.review.action", command, actor=args.actor, start=start, events=[evt], data={"review": args.review, "action": action, "status": review_status, "pending_reviewers": assignment["pending_reviewers"]})


def cmd_review_mark_risk(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    require_object_access(store, review["changeset_id"], actor=args.actor, action="changeset.write")
    action, evt = record_review_action(
        store,
        review,
        actor=args.actor,
        kind="risk_marked",
        body=args.body,
        metadata={"risk": args.risk},
        risk_summary=args.risk,
    )
    return envelope("tri.review.action", command, actor=args.actor, start=start, events=[evt], data={"review": args.review, "action": action, "status": store.get_review(args.review)["status"]})


def cmd_review_attach_policy_finding(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    cs = store.get_changeset(review["changeset_id"])
    require_object_access(store, cs["id"], actor=args.actor, action="changeset.write")
    decision, diagnostics = evaluate_export_policy(store, cs, actor=args.actor, action="export.review")
    policy_evt = store.record_policy_decision(actor=args.actor, action="export.review", object_id=cs["id"], decision=decision, project_id=cs["project_id"])
    metadata = {"decision": decision, "diagnostics": diagnostics}
    action, evt = record_review_action(store, review, actor=args.actor, kind="policy_finding_attached", body=args.body, metadata=metadata)
    return envelope(
        "tri.review.action",
        command,
        actor=args.actor,
        start=start,
        events=[policy_evt, evt],
        data={"review": args.review, "action": action, "status": store.get_review(args.review)["status"], "policy_finding": metadata},
    )


def cmd_review_attach_check_summary(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    cs = store.get_changeset(review["changeset_id"])
    require_object_access(store, cs["id"], actor=args.actor, action="changeset.write")
    summary = changeset_check_summary(store, cs)
    review_status = review_status_for_check_summary(summary) if review["status"] in ("open", "waiting_for_checks") else None
    action, evt = record_review_action(
        store,
        review,
        actor=args.actor,
        kind="check_summary_attached",
        body=args.body,
        metadata={"check_summary": summary},
        review_status=review_status,
        check_summary=summary,
    )
    return envelope(
        "tri.review.action",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={"review": args.review, "action": action, "status": store.get_review(args.review)["status"], "check_summary": summary},
    )


def cmd_review_close(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    require_object_access(store, review["changeset_id"], actor=args.actor, action="changeset.write")
    action, evt = record_review_action(
        store,
        review,
        actor=args.actor,
        kind="closed",
        body=args.body,
        metadata={"previous_status": review["status"]},
        review_status="closed",
    )
    return envelope("tri.review.action", command, actor=args.actor, start=start, events=[evt], data={"review": args.review, "action": action, "status": "closed", "previous_status": review["status"]})


def cmd_review_request_agent_pass(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    review = store.get_review(args.review)
    require_review_open(review)
    cs = store.get_changeset(review["changeset_id"])
    require_object_access(store, cs["id"], actor=args.actor, action="changeset.write")
    task_id = new_id("task")
    workspace_id = new_id("ws")
    run_id = new_id("run")
    assignee = args.assignee or args.actor
    now = utcnow()
    with store.conn:
        store.conn.execute(
            "INSERT INTO tasks (id, project_id, title, body, requester, acceptance_criteria_json, required_checks_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, cs["project_id"], args.task, "", args.actor, dumps([]), dumps([]), "running", now),
        )
        store.conn.execute(
            """
            INSERT INTO workspaces (
              id, project_id, base_snapshot_id, head_snapshot_id, owner_actor,
              task_id, status, visibility_json, hidden_paths_json, advisory_id, materialized_path, materialized_snapshot_id,
              ttl, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                cs["project_id"],
                cs["base_snapshot_id"],
                cs["head_snapshot_id"],
                assignee,
                task_id,
                "active",
                cs["visibility_json"],
                cs["hidden_paths_json"],
                cs["advisory_id"],
                None,
                None,
                None,
                now,
                now,
            ),
        )
        store.conn.execute(
            "INSERT INTO task_runs (id, task_id, actor, workspace_id, status, final_recommendation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, assignee, workspace_id, "started", None, now),
        )
        workspace_evt = store.create_event(
            kind="review.agent_pass_workspace_created",
            actor=args.actor,
            project_id=cs["project_id"],
            workspace_id=workspace_id,
            task_id=task_id,
            object_refs={"review": review["id"], "changeset": cs["id"], "task": task_id, "workspace": workspace_id, "task_run": run_id, "assignee": assignee},
            input_fingerprints={"base_snapshot": cs["base_snapshot_id"], "head_snapshot": cs["head_snapshot_id"]},
            output_fingerprints={"workspace_head": cs["head_snapshot_id"]},
        )
    action, evt = record_review_action(
        store,
        review,
        actor=args.actor,
        kind="agent_pass_requested",
        body=args.task,
        metadata={"task": task_id, "workspace": workspace_id, "task_run": run_id, "assignee": assignee},
        review_status="agent_pass_requested",
    )
    return envelope(
        "tri.review.agent_pass",
        command,
        actor=args.actor,
        start=start,
        events=[workspace_evt, evt],
        data={
            "review": args.review,
            "action": action,
            "task": task_id,
            "workspace": workspace_id,
            "task_run": run_id,
            "changeset": cs["id"],
            "base_snapshot": cs["base_snapshot_id"],
            "head_snapshot": cs["head_snapshot_id"],
        },
    )


def required_check_diagnostics(store: Store, cs: sqlite3.Row) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    diagnostics: list[dict[str, Any]] = []
    checks = [check_data(store, check_id) for check_id in loads(cs["checks_json"], [])]
    latest_check_by_command: dict[str, dict[str, Any]] = {}
    for check in checks:
        latest_check_by_command[dumps(check["command"])] = check
    required_checks = task_required_checks_for_changeset(store, cs)
    if required_checks:
        latest_required = []
        missing = []
        failing = []
        stale = []
        for spec in required_checks:
            command_key = dumps(spec["command"])
            check = latest_check_by_command.get(command_key)
            if check is None:
                missing.append(spec)
                continue
            latest_required.append(check)
            if check["status"] != "passed":
                failing.append(check["id"])
            if check["snapshot_after"] != cs["head_snapshot_id"]:
                stale.append(check["id"])
        if missing:
            diagnostics.append(decision_denial("missing_required_check", "Task required checks are missing.", object=cs["id"], required_checks=missing, reason="missing"))
        if failing:
            diagnostics.append(decision_denial("missing_required_check", "Task required checks are not passing.", object=cs["id"], checks=failing, reason="failing"))
        if stale:
            diagnostics.append(decision_denial("missing_required_check", "Task required checks are stale for the changeset head.", object=cs["id"], checks=stale, reason="stale"))
        return diagnostics, latest_required, len(checks)
    if not latest_check_by_command:
        diagnostics.append(decision_denial("missing_required_check", "At least one passing check is required before export.", object=cs["id"], reason="missing"))
    else:
        failing = [check["id"] for check in latest_check_by_command.values() if check["status"] != "passed"]
        stale = [check["id"] for check in latest_check_by_command.values() if check["snapshot_after"] != cs["head_snapshot_id"]]
        if failing:
            diagnostics.append(decision_denial("missing_required_check", "Required checks are not passing.", object=cs["id"], checks=failing, reason="failing"))
        if stale:
            diagnostics.append(decision_denial("missing_required_check", "Required checks are stale for the changeset head.", object=cs["id"], checks=stale, reason="stale"))
    return diagnostics, list(latest_check_by_command.values()), len(checks)


def task_required_checks_for_changeset(store: Store, cs: sqlite3.Row) -> list[dict[str, Any]]:
    task_id = changeset_task_id(store, cs)
    if task_id is None:
        return []
    task = store.row("SELECT required_checks_json FROM tasks WHERE id = ?", (task_id,))
    return loads(task["required_checks_json"], []) if task else []


def non_exportable_artifacts_for_changeset(store: Store, cs: sqlite3.Row) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in store.rows("SELECT * FROM artifacts WHERE labels_json LIKE ? ORDER BY created_at, rowid", ("%non-exportable%",)):
        labels = loads(row["labels_json"], [])
        if "non-exportable" not in labels:
            continue
        origins = []
        for origin in artifact_origins(store, row["id"]):
            if origin.get("changeset") == cs["id"]:
                origins.append(origin)
                continue
            if origin.get("workspace") == cs["workspace_id"] and origin.get("created_at", "") <= cs["created_at"]:
                origins.append(origin)
        if not origins or row["id"] in seen:
            continue
        seen.add(row["id"])
        blocked.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "labels": labels,
                "origins": origins,
            }
        )
    return blocked


def unresolved_conflicts_for_changeset(store: Store, cs: sqlite3.Row, *, actor: str, hidden_patterns: list[str]) -> list[dict[str, Any]]:
    if not cs["workspace_id"]:
        return []
    rows = store.rows(
        "SELECT * FROM workspace_conflicts WHERE workspace_id = ? AND status = ? AND created_at <= ? ORDER BY created_at, rowid",
        (cs["workspace_id"], "open", cs["created_at"]),
    )
    conflicts = []
    try:
        workspace_decision, _ = object_access_decision(store, cs["workspace_id"], actor=actor, action="workspace.read")
    except TriError:
        workspace_decision = {"allowed": False}
    for row in rows:
        if not workspace_decision["allowed"]:
            conflicts.append({"redacted": True, "required_grant": "workspace.read", "status": row["status"]})
            continue
        conflicts.append(
            {
                "id": row["id"],
                "status": row["status"],
                "target_ref": row["target_ref"],
                "conflicts": [conflict_policy_view(item, hidden_patterns) for item in loads(row["conflicts_json"], [])],
                "required_resolution": ["tri", "conflicts", "resolve", row["id"], "--strategy", "ours|theirs|base"],
                "required_policy_checks": ["workspace.write"],
            }
        )
    return conflicts


def evaluate_export_policy(
    store: Store,
    cs: sqlite3.Row,
    *,
    actor: str,
    action: str,
    override_reason: str | None = None,
    disclosed_advisories: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policies = [
        "policy_changeset_lifecycle",
        "policy_workspace_lifecycle",
        "policy_no_secret_export",
        "policy_path_visibility_export",
        "policy_no_unresolved_conflicts",
        "policy_stack_dependency_satisfied",
        "policy_advisory_embargo",
        "policy_required_checks_passed",
        "policy_review_approved",
    ]
    grants: list[str] = []
    denials: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    redactions: list[dict[str, Any]] = []
    access_action = "changeset.read"
    if action == "changeset.merge":
        access_action = "changeset.merge"
    elif action == "release.prepare":
        access_action = "changeset.export"
    elif action.startswith("export."):
        access_action = "changeset.export"
    access_decision, access_diagnostics = object_access_decision(store, cs["id"], actor=actor, action=access_action)
    diagnostics.extend(access_diagnostics)
    denials.extend(diagnostic["code"] for diagnostic in access_diagnostics)
    grants.extend(access_decision["grants"])
    changeset_lifecycle = {
        "status": cs["status"],
        "replacement": cs["replacement_changeset_id"],
        "metadata": loads(cs["lifecycle_json"], {}),
    }
    if cs["status"] in ("discarded", "superseded") and (action.startswith("export.") or action in ("changeset.merge", "release.prepare")):
        diagnostics.append(
            decision_denial(
                "changeset_lifecycle_blocked",
                "Discarded or superseded changesets cannot merge, release, or export.",
                object=cs["id"],
                status=cs["status"],
                replacement=cs["replacement_changeset_id"],
            )
        )
        denials.append("changeset_lifecycle_blocked")
    workspace_lifecycle = None
    if cs["workspace_id"]:
        workspace = store.get_workspace(cs["workspace_id"])
        workspace_lifecycle = {"workspace": workspace["id"], "status": workspace["status"]}
        if workspace["status"] == "discarded" and (action.startswith("export.") or action in ("changeset.merge", "release.prepare")):
            diagnostics.append(
                decision_denial(
                    "workspace_lifecycle_blocked",
                    "Discarded workspaces cannot merge, release, or export changesets.",
                    object=cs["id"],
                    workspace=workspace["id"],
                    status=workspace["status"],
                )
            )
            denials.append("workspace_lifecycle_blocked")
    hidden_patterns = effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=actor)
    unresolved_conflicts = unresolved_conflicts_for_changeset(store, cs, actor=actor, hidden_patterns=hidden_patterns)
    for conflict in unresolved_conflicts:
        diagnostics.append(
            decision_denial(
                "unresolved_conflict",
                "Changeset cannot merge, release, or export while its source workspace has unresolved conflicts.",
                object=cs["id"],
                conflict=conflict.get("id"),
                conflict_redacted=bool(conflict.get("redacted")),
                required_resolution=conflict.get("required_resolution", ["tri", "conflicts", "resolve"]),
            )
        )
        denials.append("unresolved_conflict")
    changed = changed_paths(store.load_snapshot_tree(cs["base_snapshot_id"]), store.load_snapshot_tree(cs["head_snapshot_id"]))
    for path in changed:
        visible, reason = path_visibility(path, hidden_patterns)
        if reason == "secret_like_path":
            diagnostics.append(decision_denial("secret_export_blocked", "Refusing to export secret-like path.", object=path))
            denials.append("secret_export_blocked")
            redactions.append({"path": path, "reason": "secret_like_path"})
        elif not visible:
            ref = redacted_path_ref(path)
            diagnostics.append(decision_denial("visibility_export_blocked", "Refusing to export a policy-hidden path.", **ref))
            denials.append("visibility_export_blocked")
            redactions.append({"path_hash": ref["path_hash"], "reason": "hidden_path"})
    non_exportable_artifacts = non_exportable_artifacts_for_changeset(store, cs)
    for artifact in non_exportable_artifacts:
        diagnostics.append(
            decision_denial(
                "non_exportable_artifact",
                "Refusing to export a changeset with a non-exportable artifact attached.",
                object=artifact["id"],
                artifact_kind=artifact["kind"],
                labels=artifact["labels"],
                origins=[{"type": origin.get("type"), "id": origin.get("id"), "policy_object": origin.get("policy_object")} for origin in artifact["origins"]],
            )
        )
        denials.append("non_exportable_artifact")
    advisory_ref = None
    if cs["advisory_id"]:
        advisory = store.get_advisory(cs["advisory_id"])
        advisory_ref = {
            "id": advisory["id"],
            "status": advisory["status"],
            "disclosure_state": advisory["disclosure_state"],
            "disclosed_for_action": advisory["id"] in (disclosed_advisories or set()),
        }
        if not advisory_is_disclosed(advisory) and advisory["id"] not in (disclosed_advisories or set()):
            diagnostics.append(
                decision_denial(
                    "advisory_embargo_active",
                    "Advisory-linked changeset cannot merge or export until coordinated disclosure is prepared.",
                    object=cs["id"],
                    advisory=advisory["id"],
                    advisory_status=advisory["status"],
                    disclosure_state=advisory["disclosure_state"],
                )
            )
            denials.append("advisory_embargo_active")
    full_stack = changeset_stack_data(store, cs)
    visible_stack = changeset_stack_data(store, cs, actor=actor)
    if full_stack["parent"] is not None and full_stack["dependency_state"] != "satisfied":
        parent_redacted = bool(visible_stack.get("parent_redacted"))
        diagnostics.append(
            decision_denial(
                "unsatisfied_changeset_dependency",
                "Changeset dependency must merge before this changeset can merge or export.",
                object=cs["id"],
                parent=None if parent_redacted else full_stack["parent"],
                parent_redacted=parent_redacted,
                dependency_state=visible_stack["dependency_state"],
                parent_status=visible_stack["parent_status"],
            )
        )
        denials.append("unsatisfied_changeset_dependency")
    check_diagnostics, latest_checks, total_checks = required_check_diagnostics(store, cs)
    approved_reviews = []
    for review_id in loads(cs["reviews_json"], []):
        review = store.get_review(review_id)
        if review["status"] == "approved":
            approved_reviews.append(review_id)
    rule = matching_merge_rule(store, cs, actor=actor, changed=changed) if action == "changeset.merge" else None
    post_merge_verification = None
    if rule is not None and rule.get("verify_after"):
        deferred = [diagnostic for diagnostic in check_diagnostics if diagnostic.get("reason") in ("missing", "stale")]
        if deferred and len(deferred) == len(check_diagnostics):
            # Nothing has failed; the rule's check set runs after merge instead.
            check_diagnostics = []
            post_merge_verification = {"check_set": rule["verify_after"], "merge_rule": rule["id"], "deferred": [diagnostic["reason"] for diagnostic in deferred]}
            grants.append(f"post_merge_verification:{rule['id']}")
    diagnostics.extend(check_diagnostics)
    denials.extend(diagnostic["code"] for diagnostic in check_diagnostics)
    merge_rule = None
    if not approved_reviews and rule is not None:
        merge_rule = rule
        grants.append(f"merge_rule:{merge_rule['id']}")
    if not approved_reviews and merge_rule is None:
        diagnostics.append(decision_denial("missing_review_approval", "An approved review is required before export.", object=cs["id"]))
        denials.append("missing_review_approval")
    overrideable = [diagnostic for diagnostic in diagnostics if diagnostic["code"] in ("missing_required_check", "missing_review_approval")]
    hard_denials = [diagnostic for diagnostic in diagnostics if diagnostic["code"] not in ("missing_required_check", "missing_review_approval")]
    override = None
    if override_reason and overrideable and not hard_denials:
        override = {"actor": actor, "reason": override_reason, "overrode": [diagnostic["code"] for diagnostic in overrideable]}
        diagnostics = []
        denials = []
        grants.append("local_export_override")
    allowed = not diagnostics
    reason_code = "allowed" if allowed else diagnostics[0]["code"]
    decision = {
        "id": new_id("decision"),
        "allowed": allowed,
        "actor": actor,
        "action": action,
        "object": cs["id"],
        "policies": policies,
        "grants": grants,
        "denials": sorted(set(denials)),
        "redactions": redactions,
        "non_exportable_artifacts": non_exportable_artifacts,
        "unresolved_conflicts": unresolved_conflicts,
        "reason_code": reason_code,
        "checks": {
            "total": total_checks,
            "latest_required": [check["id"] for check in latest_checks],
            "passed": sum(1 for check in latest_checks if check["status"] == "passed"),
        },
        "review": {"approved": bool(approved_reviews), "approved_reviews": approved_reviews, "merge_rule": merge_rule["id"] if merge_rule else None},
        "post_merge_verification": post_merge_verification,
        "stack": visible_stack,
        "advisory": advisory_ref,
        "changeset_lifecycle": changeset_lifecycle,
        "workspace_lifecycle": workspace_lifecycle,
        "object_access": access_decision,
        "override": override,
        "event": None,
    }
    return decision, diagnostics


def cmd_policy_evaluate_export(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    decision, diagnostics = evaluate_export_policy(store, cs, actor=args.actor, action="export.git", override_reason=args.override_reason)
    evt = store.record_policy_decision(actor=args.actor, action="export.git", object_id=cs["id"], decision=decision, project_id=cs["project_id"])
    return envelope("tri.policy.decision", command, actor=args.actor, start=start, ok=decision["allowed"], events=[evt], diagnostics=diagnostics, data=decision)


def require_policy_access_action(requested: str | None, default: str, *, object_id: str, object_type: str) -> str:
    action = requested or default
    if action != default:
        raise TriError(
            "invalid_command",
            f"{object_type} policy evaluation only supports {default}.",
            object=object_id,
            action=action,
            accepted_actions=[default],
        )
    return action


def evidence_policy_decision_view(store: Store, decision: dict[str, Any], *, actor: str) -> dict[str, Any]:
    view = copy.deepcopy(decision)
    view.setdefault("visibility", ["derived"])
    view.setdefault("owner", None)
    view.setdefault("actor_selectors", actor_selectors(actor))
    view.setdefault("matched_grants", [])
    redacted_refs = [0]
    view = redact_policy_payload_refs(store, view, actor=actor, counter=redacted_refs)
    view["object"] = decision["object"]
    if redacted_refs[0]:
        view["redacted_refs"] = redacted_refs[0]
    return view


def check_read_diagnostics(decision: dict[str, Any]) -> list[dict[str, Any]]:
    if decision["allowed"]:
        return []
    return [
        decision_denial(
            "policy_denied",
            "Actor is not allowed to access this check.",
            object=decision["object"],
            action="check.read",
            required_grant="check.read",
        )
    ]


def event_read_diagnostics(decision: dict[str, Any]) -> list[dict[str, Any]]:
    if decision["allowed"]:
        return []
    required_actions = sorted({candidate["action"] for candidate in decision.get("policy_candidates", [])})
    return [
        decision_denial(
            "policy_denied",
            "Actor is not allowed to access this event.",
            object=decision["object"],
            action="event.read",
            policy_candidate_count=len(decision.get("policy_candidates", [])),
            redacted_candidates=len(decision.get("policy_candidates", [])),
            required_grants=required_actions,
        )
    ]


def cmd_policy_evaluate_access(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.object.startswith("run_"):
        action = require_policy_access_action(args.action, "task_run.read", object_id=args.object, object_type="Task-run")
        row = get_task_run(store, args.object)
        decision = task_run_access_decision(store, row, actor=args.actor)
        diagnostics = task_run_access_diagnostics(decision)
        ctx = task_run_event_context(store, row)
        evt = store.record_policy_decision(
            actor=args.actor,
            action=action,
            object_id=args.object,
            decision=decision,
            project_id=ctx["project"],
            workspace_id=ctx["workspace"],
            task_id=ctx["task"],
        )
        data = task_run_access_decision_view(store, row, decision, actor=args.actor)
        return envelope("tri.policy.decision", command, actor=args.actor, start=start, ok=decision["allowed"], events=[evt], diagnostics=diagnostics, data=data)
    if args.object.startswith("artifact_"):
        action = require_policy_access_action(args.action, "artifact.read", object_id=args.object, object_type="Artifact")
        decision, diagnostics = artifact_access_decision(store, args.object, actor=args.actor)
        ctx = artifact_event_context(store, args.object)
        evt = store.record_policy_decision(
            actor=args.actor,
            action=action,
            object_id=args.object,
            decision=decision,
            project_id=ctx["project"],
            workspace_id=ctx["workspace"],
            task_id=ctx["task"],
        )
        data = evidence_policy_decision_view(store, decision, actor=args.actor)
        return envelope("tri.policy.decision", command, actor=args.actor, start=start, ok=decision["allowed"], events=[evt], diagnostics=diagnostics, data=data)
    if args.object.startswith("check_"):
        action = require_policy_access_action(args.action, "check.read", object_id=args.object, object_type="Check")
        check = check_data(store, args.object)
        origin_decision = check_access_decision(store, check, actor=args.actor)
        decision = check_read_policy_decision(check, origin_decision, actor=args.actor)
        diagnostics = check_read_diagnostics(decision)
        ctx = check_event_context(store, check)
        evt = store.record_policy_decision(
            actor=args.actor,
            action=action,
            object_id=args.object,
            decision=decision,
            project_id=ctx["project"],
            workspace_id=ctx["workspace"],
            task_id=ctx["task"],
        )
        data = evidence_policy_decision_view(store, decision, actor=args.actor)
        return envelope("tri.policy.decision", command, actor=args.actor, start=start, ok=decision["allowed"], events=[evt], diagnostics=diagnostics, data=data)
    if args.object.startswith("evt_"):
        action = require_policy_access_action(args.action, "event.read", object_id=args.object, object_type="Event")
        row = store.row("SELECT * FROM events WHERE id = ?", (args.object,))
        if row is None:
            raise TriError("path_not_found", "Event not found.", object=args.object)
        event = event_data(row)
        decision = event_access_decision(store, event, actor=args.actor)
        diagnostics = event_read_diagnostics(decision)
        evt = store.record_policy_decision(
            actor=args.actor,
            action=action,
            object_id=args.object,
            decision=decision,
            project_id=event.get("project"),
            workspace_id=event.get("workspace"),
            task_id=event.get("task"),
        )
        data = evidence_policy_decision_view(store, decision, actor=args.actor)
        return envelope("tri.policy.decision", command, actor=args.actor, start=start, ok=decision["allowed"], events=[evt], diagnostics=diagnostics, data=data)
    ctx = object_policy_context(store, args.object)
    action = args.action or default_grant_actions(ctx["type"])[0]
    decision, diagnostics = object_access_decision(store, args.object, actor=args.actor, action=action)
    evt = store.record_policy_decision(actor=args.actor, action=action, object_id=args.object, decision=decision, project_id=ctx["project"], workspace_id=ctx.get("workspace"))
    return envelope("tri.policy.decision", command, actor=args.actor, start=start, ok=decision["allowed"], events=[evt], diagnostics=diagnostics, data=decision)


def policy_decision_event(store: Store, decision_id: str) -> dict[str, Any]:
    policy_kinds = ("policy.evaluated", "policy.denied")
    for kind in policy_kinds:
        for row in store.rows("SELECT * FROM events WHERE kind = ? AND object_refs_json LIKE ? ORDER BY created_at DESC, rowid DESC", (kind, f"%{decision_id}%")):
            event = event_data(row)
            if event["object_refs"].get("decision") == decision_id or event["policy_result"].get("id") == decision_id:
                return event
    for kind in policy_kinds:
        for row in store.rows("SELECT * FROM events WHERE kind = ? ORDER BY created_at DESC, rowid DESC", (kind,)):
            event = event_data(row)
            if event["object_refs"].get("decision") == decision_id or event["policy_result"].get("id") == decision_id:
                return event
    raise TriError("path_not_found", "Policy decision not found.", object=decision_id)


def require_policy_decision_explain_access(store: Store, event: dict[str, Any], *, actor: str) -> None:
    if actor == event["actor"] or "role:maintainer" in actor_selectors(actor):
        return
    object_id = event["object_refs"].get("object") or event["policy_result"].get("object")
    if not object_id:
        raise TriError("policy_denied", "Actor is not allowed to explain this policy decision.", object=event["id"])
    try:
        if str(object_id).startswith("artifact_"):
            decision, _ = artifact_access_decision(store, str(object_id), actor=actor)
        elif str(object_id).startswith("check_"):
            decision = check_access_decision(store, check_data(store, str(object_id)), actor=actor)
        elif str(object_id).startswith("evt_"):
            row = store.row("SELECT * FROM events WHERE id = ?", (str(object_id),))
            decision = {"allowed": False} if row is None else event_access_decision(store, event_data(row), actor=actor)
        elif str(object_id).startswith("run_"):
            row = store.row("SELECT * FROM task_runs WHERE id = ?", (str(object_id),))
            decision = {"allowed": False} if row is None else task_run_access_decision(store, row, actor=actor)
        else:
            ctx = object_policy_context(store, str(object_id))
            action = default_grant_actions(ctx["type"])[0]
            decision, _ = object_access_decision(store, str(object_id), actor=actor, action=action)
    except TriError:
        decision = {"allowed": False}
    if not decision["allowed"]:
        raise TriError("policy_denied", "Actor is not allowed to explain this policy decision.", object=event["id"], decision=event["object_refs"].get("decision"))


def cmd_policy_explain(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    event = policy_decision_event(store, args.decision)
    require_policy_decision_explain_access(store, event, actor=args.actor)
    visible_event = event_view_data(store, event, actor=args.actor)
    object_refs = event["object_refs"]
    data = {
        "id": args.decision,
        "decision": visible_event["policy_result"],
        "event": visible_event,
        "action": object_refs.get("action") or event["policy_result"].get("action"),
        "object": object_refs.get("object") or event["policy_result"].get("object"),
        "recorded_at": event["created_at"],
    }
    return envelope("tri.policy.explain", command, actor=args.actor, start=start, data=data)


def cmd_policy_create_embargo(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    body = Path(args.body_file).read_text() if args.body_file else (args.body or "")
    data, evt = create_advisory_record(
        store,
        project=project,
        actor=args.actor,
        title=args.title,
        body=body,
        affected_packages=sorted(set(args.affected_package or [])),
        trusted_reviewers=sorted({normalize_principal(item) for item in args.trusted_reviewer or []}),
        embargoed=True,
        event_kind="policy.embargo_created",
    )
    return envelope("tri.policy.embargo", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_policy_disclose_embargo(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return prepare_release(store, args, command, start, schema="tri.policy.embargo_disclosure")


def policy_visibility_data(store: Store, object_id: str, *, actor: str) -> dict[str, Any]:
    ctx = object_policy_context(store, object_id)
    hidden_paths: list[str] = []
    parent_refs: dict[str, Any] = {}
    if ctx["type"] == "workspace":
        ws = store.get_workspace(object_id)
        hidden_paths = loads(ws["hidden_paths_json"], [])
    elif ctx["type"] == "package":
        hidden_paths = ctx.get("path_globs", [])
    elif ctx["type"] == "changeset":
        cs = store.get_changeset(object_id)
        hidden_paths = loads(cs["hidden_paths_json"], [])
        if cs["workspace_id"]:
            decision, _ = object_access_decision(store, cs["workspace_id"], actor=actor, action="workspace.read")
            if decision["allowed"]:
                parent_refs["workspace"] = cs["workspace_id"]
            else:
                parent_refs["workspace_redacted"] = True
    elif ctx["type"] == "review":
        if ctx.get("changeset"):
            decision, _ = object_access_decision(store, ctx["changeset"], actor=actor, action="changeset.read")
            if decision["allowed"]:
                parent_refs["changeset"] = ctx["changeset"]
            else:
                parent_refs["changeset_redacted"] = True
        if ctx.get("workspace"):
            decision, _ = object_access_decision(store, ctx["workspace"], actor=actor, action="workspace.read")
            if decision["allowed"]:
                parent_refs["workspace"] = ctx["workspace"]
            else:
                parent_refs["workspace_redacted"] = True
    inherited_grants = inherited_grants_for_object(store, ctx, action=f"{ctx['type']}.read")
    if ctx["type"] != "workspace" and ctx.get("workspace"):
        parent_decision, _ = object_access_decision(store, ctx["workspace"], actor=actor, action="workspace.read")
        if not parent_decision["allowed"]:
            inherited_grants = []
    return {
        "object": object_id,
        "object_type": ctx["type"],
        "project": ctx["project"],
        "workspace": ctx.get("workspace") if not parent_refs.get("workspace_redacted") else None,
        "parents": parent_refs,
        "visibility": ctx["visibility"] or ["private"],
        "hidden_paths": hidden_paths,
        "owner": ctx["owner"],
        "actor": actor,
        "actor_selectors": actor_selectors(actor),
        "direct_grants": active_grants_for_object(store, object_id),
        "inherited_grants": inherited_grants,
    }


def unique_policy_grants(grants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for grant in grants:
        key = grant.get("id") or dumps(grant, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(grant)
    return unique


def inherited_grants_from_matched_origins(matched_origins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grants: list[dict[str, Any]] = []
    for origin in matched_origins:
        grants.extend(origin.get("matched_grants", []))
    return unique_policy_grants(grants)


def hidden_paths_for_visible_origins(store: Store, origins: list[dict[str, Any]], *, actor: str) -> list[str]:
    hidden_paths: list[str] = []
    for origin in origins:
        changeset_id = origin.get("changeset")
        workspace_id = origin.get("workspace")
        if isinstance(changeset_id, str):
            try:
                cs = store.get_changeset(changeset_id)
                hidden_paths.extend(effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=actor))
                continue
            except TriError:
                pass
        if isinstance(workspace_id, str):
            try:
                ws = store.get_workspace(workspace_id)
                hidden_paths.extend(effective_hidden_patterns(store, ws["project_id"], loads(ws["hidden_paths_json"], []), actor=actor))
            except TriError:
                pass
    return sorted(set(hidden_paths))


def artifact_policy_visibility_data(store: Store, artifact_id: str, *, actor: str) -> dict[str, Any]:
    decision, origins = require_artifact_access(store, artifact_id, actor=actor)
    project_id = next((origin.get("project") for origin in origins if origin.get("project")), None)
    workspace_ids = sorted({origin["workspace"] for origin in origins if isinstance(origin.get("workspace"), str)})
    changeset_ids = sorted({origin["changeset"] for origin in origins if isinstance(origin.get("changeset"), str)})
    parents: dict[str, Any] = {
        "origins": [
            {
                key: origin.get(key)
                for key in ("type", "id", "field", "policy_object", "policy_action", "workspace", "changeset")
                if key in origin
            }
            for origin in origins
        ],
        "origin_count": decision.get("origin_count", len(origins)),
        "redacted_origins": decision.get("redacted_origins", 0),
    }
    if workspace_ids:
        parents["workspaces"] = workspace_ids
    if changeset_ids:
        parents["changesets"] = changeset_ids
    data = {
        "object": artifact_id,
        "object_type": "artifact",
        "project": project_id,
        "workspace": workspace_ids[0] if len(workspace_ids) == 1 else None,
        "parents": parents,
        "visibility": ["derived"],
        "hidden_paths": hidden_paths_for_visible_origins(store, origins, actor=actor),
        "owner": None,
        "actor": actor,
        "actor_selectors": actor_selectors(actor),
        "direct_grants": active_grants_for_object(store, artifact_id),
        "inherited_grants": inherited_grants_from_matched_origins(decision.get("matched_origins", [])),
        "origins": origins,
        "access": evidence_policy_decision_view(store, decision, actor=actor),
    }
    return data


def check_policy_visibility_data(store: Store, check_id: str, *, actor: str) -> dict[str, Any]:
    check = check_data(store, check_id)
    origin_decision = require_check_access(store, check, actor=actor)
    decision = check_read_policy_decision(check, origin_decision, actor=actor)
    ctx = check_event_context(store, check)
    parents: dict[str, Any] = {}
    inherited_grants = unique_policy_grants(origin_decision.get("matched_grants", []))
    visible_linked_changesets: list[str] = []
    if check.get("workspace"):
        workspace_decision, _ = object_access_decision(store, check["workspace"], actor=actor, action="workspace.read")
        if workspace_decision["allowed"]:
            parents["workspace"] = check["workspace"]
            inherited_grants = unique_policy_grants([*inherited_grants, *workspace_decision.get("matched_grants", [])])
        else:
            parents["workspace_redacted"] = True
    if check.get("changeset"):
        changeset_decision, _ = object_access_decision(store, check["changeset"], actor=actor, action="changeset.read")
        if changeset_decision["allowed"]:
            parents["changeset"] = check["changeset"]
            inherited_grants = unique_policy_grants([*inherited_grants, *changeset_decision.get("matched_grants", [])])
        else:
            parents["changeset_redacted"] = True
    for changeset_id in linked_changesets_for_check(store, check_id):
        changeset_decision, _ = object_access_decision(store, changeset_id, actor=actor, action="changeset.read")
        if changeset_decision["allowed"]:
            visible_linked_changesets.append(changeset_id)
    if visible_linked_changesets:
        parents["linked_changesets"] = visible_linked_changesets
    return {
        "object": check_id,
        "object_type": "check",
        "project": ctx["project"],
        "workspace": check.get("workspace") if not parents.get("workspace_redacted") else None,
        "task": ctx["task"],
        "parents": parents,
        "visibility": ["derived"],
        "hidden_paths": check_hidden_patterns(store, check, actor=actor),
        "owner": check.get("actor"),
        "actor": actor,
        "actor_selectors": actor_selectors(actor),
        "direct_grants": active_grants_for_object(store, check_id),
        "inherited_grants": inherited_grants,
        "access": evidence_policy_decision_view(store, decision, actor=actor),
    }


def require_event_policy_access(store: Store, event: dict[str, Any], *, actor: str) -> dict[str, Any]:
    decision = event_access_decision(store, event, actor=actor)
    if decision["allowed"]:
        return decision
    required_actions = sorted({candidate["action"] for candidate in decision["policy_candidates"]})
    evt = store.record_policy_decision(
        actor=actor,
        action="event.read",
        object_id=event["id"],
        decision=decision,
        project_id=event.get("project"),
        workspace_id=event.get("workspace"),
        task_id=event.get("task"),
        kind="policy.denied",
    )
    raise TriError(
        "policy_denied",
        "Actor is not allowed to access this event.",
        object=event["id"],
        action="event.read",
        policy_candidate_count=len(decision["policy_candidates"]),
        redacted_candidates=len(decision["policy_candidates"]),
        required_grants=required_actions,
        decision=decision["id"],
        event=evt,
        explain_command=["tri", "policy", "explain", decision["id"]],
    )


def event_policy_visibility_data(store: Store, event_id: str, *, actor: str) -> dict[str, Any]:
    row = store.row("SELECT * FROM events WHERE id = ?", (event_id,))
    if row is None:
        raise TriError("path_not_found", "Event not found.", object=event_id)
    event = event_data(row)
    decision = require_event_policy_access(store, event, actor=actor)
    visible_event = event_view_data(store, event, actor=actor, record_denial=False)
    parents: dict[str, Any] = {}
    hidden_paths: list[str] = []
    if visible_event.get("workspace"):
        parents["workspace"] = visible_event["workspace"]
        try:
            ws = store.get_workspace(visible_event["workspace"])
            hidden_paths.extend(effective_hidden_patterns(store, ws["project_id"], loads(ws["hidden_paths_json"], []), actor=actor))
        except TriError:
            pass
    elif visible_event.get("redacted_workspace"):
        parents["workspace_redacted"] = True
    if visible_event.get("task"):
        task_decision, _ = object_access_decision(store, visible_event["task"], actor=actor, action="task.read")
        if task_decision["allowed"]:
            parents["task"] = visible_event["task"]
        else:
            parents["task_redacted"] = True
    inherited_grants: list[dict[str, Any]] = []
    for candidate in decision.get("matched_candidates", []):
        inherited_grants.extend(candidate.get("matched_grants", []))
    return {
        "object": event_id,
        "object_type": "event",
        "project": visible_event.get("project"),
        "workspace": visible_event.get("workspace"),
        "task": visible_event.get("task") if not parents.get("task_redacted") else None,
        "parents": parents,
        "visibility": ["derived"],
        "hidden_paths": sorted(set(hidden_paths)),
        "owner": event.get("actor"),
        "actor": actor,
        "actor_selectors": actor_selectors(actor),
        "direct_grants": active_grants_for_object(store, event_id),
        "inherited_grants": unique_policy_grants(inherited_grants),
        "access": evidence_policy_decision_view(store, decision, actor=actor),
    }


def cmd_policy_visibility(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.object.startswith("run_"):
        row = get_task_run(store, args.object)
        require_task_run_access(store, row, actor=args.actor)
        data = task_run_policy_visibility_data(store, row, actor=args.actor)
        return envelope("tri.policy.visibility", command, actor=args.actor, start=start, data=data)
    if args.object.startswith("artifact_"):
        data = artifact_policy_visibility_data(store, args.object, actor=args.actor)
        return envelope("tri.policy.visibility", command, actor=args.actor, start=start, data=data)
    if args.object.startswith("check_"):
        data = check_policy_visibility_data(store, args.object, actor=args.actor)
        return envelope("tri.policy.visibility", command, actor=args.actor, start=start, data=data)
    if args.object.startswith("evt_"):
        data = event_policy_visibility_data(store, args.object, actor=args.actor)
        return envelope("tri.policy.visibility", command, actor=args.actor, start=start, data=data)
    ctx = object_policy_context(store, args.object)
    require_object_access(store, args.object, actor=args.actor, action=default_grant_actions(ctx["type"])[0])
    data = policy_visibility_data(store, args.object, actor=args.actor)
    return envelope("tri.policy.visibility", command, actor=args.actor, start=start, data=data)


def cmd_policy_grant(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can create policy grants.", object=args.object)
    ctx = object_policy_context(store, args.object)
    principal = normalize_principal(args.principal)
    actions = args.action or default_grant_actions(ctx["type"])
    grant_id = new_id("grant")
    with store.conn:
        store.conn.execute(
            "INSERT INTO policy_grants (id, project_id, object_type, object_id, principal, actions_json, status, created_by, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (grant_id, ctx["project"], ctx["type"], args.object, principal, dumps(actions), "active", args.actor, utcnow(), None),
        )
        evt = store.create_event(
            kind="policy.grant_created",
            actor=args.actor,
            project_id=ctx["project"],
            workspace_id=ctx.get("workspace"),
            object_refs={"grant": grant_id, "object": args.object, "principal": principal, "actions": actions},
        )
    return envelope("tri.policy.grant", command, actor=args.actor, start=start, events=[evt], data=grant_data(store.get_policy_grant(grant_id)))


def cmd_policy_grant_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    grant = store.get_policy_grant(args.grant)
    require_policy_grant_inspect_access(store, grant, actor=args.actor)
    return envelope("tri.policy.grant", command, actor=args.actor, start=start, data=grant_data(grant))


def cmd_policy_revoke(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    grant = store.get_policy_grant(args.grant)
    if "role:maintainer" not in actor_selectors(args.actor) and grant["created_by"] != args.actor:
        raise TriError("policy_denied", "Only maintainers or grant creators can revoke policy grants.", object=args.grant)
    ctx = object_policy_context(store, grant["object_id"])
    with store.conn:
        store.conn.execute("UPDATE policy_grants SET status = ?, revoked_at = ? WHERE id = ?", ("revoked", utcnow(), grant["id"]))
        evt = store.create_event(
            kind="policy.grant_revoked",
            actor=args.actor,
            project_id=grant["project_id"],
            workspace_id=ctx.get("workspace"),
            object_refs={"grant": grant["id"], "object": grant["object_id"], "principal": grant["principal"]},
        )
    return envelope("tri.policy.grant", command, actor=args.actor, start=start, events=[evt], data=grant_data(store.get_policy_grant(args.grant)))


def export_source_events(store: Store, export: sqlite3.Row, *, actor: str | None = None, include_events: bool = True) -> dict[str, Any]:
    cs = store.get_changeset(export["changeset_id"])
    operation_ids = set(loads(cs["operations_json"], []))
    check_ids = set(loads(cs["checks_json"], []))
    review_ids = set(loads(cs["reviews_json"], []))
    policy_ids = set(loads(cs["policy_decisions_json"], []))
    hidden_patterns = effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=actor)

    def redact_paths(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {item_key: redact_paths(item_value, item_key) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [redact_paths(item, key) for item in value]
        if isinstance(value, str) and key in {"path", "paths", "src", "dest"}:
            visible, reason = path_visibility(value, hidden_patterns)
            if not visible:
                return {**redacted_path_ref(value), "redaction": reason}
        return value

    def export_event_data(row: sqlite3.Row) -> dict[str, Any]:
        event = event_data(row)
        event["object_refs"] = redact_paths(event["object_refs"])
        event["policy_result"] = redact_paths(event["policy_result"])
        if actor is not None:
            workspace_id, redacted_workspace = workspace_ref_for_actor(store, event["workspace"], actor=actor)
            event["workspace"] = workspace_id
            if redacted_workspace:
                event["workspace_redacted"] = True
            redacted_refs = [0]
            event["object_refs"] = redact_event_sensitive_refs(store, event["object_refs"], actor=actor, counter=redacted_refs)
            event["input_fingerprints"] = redact_event_sensitive_refs(store, event["input_fingerprints"], actor=actor, counter=redacted_refs)
            event["output_fingerprints"] = redact_event_sensitive_refs(store, event["output_fingerprints"], actor=actor, counter=redacted_refs)
            event["policy_result"] = redact_policy_payload_refs(store, event["policy_result"], actor=actor, counter=redacted_refs)
            if redacted_refs[0]:
                event["redacted_object_refs"] = redacted_refs[0]
        return event

    rows = []
    for row in store.rows(
        "SELECT * FROM events WHERE project_id = ? AND created_at <= ? ORDER BY created_at, rowid",
        (cs["project_id"], export["completed_at"] or export["created_at"]),
    ):
        refs = loads(row["object_refs_json"], {})
        if refs.get("changeset") == cs["id"] or refs.get("export") == export["id"]:
            rows.append(row)
            continue
        if cs["workspace_id"] and row["workspace_id"] == cs["workspace_id"]:
            if row["kind"].startswith(("workspace.", "check.", "config.", "secret.", "review.")):
                rows.append(row)
                continue
        if refs.get("operation") in operation_ids or refs.get("check") in check_ids or refs.get("review") in review_ids:
            rows.append(row)
            continue
        if refs.get("decision") in policy_ids:
            rows.append(row)
    if not include_events:
        return {
            "count": len(rows),
            "first": rows[0]["id"] if rows else None,
            "last": rows[-1]["id"] if rows else None,
        }
    events = [export_event_data(row) for row in rows]
    return {
        "count": len(events),
        "first": events[0]["id"] if events else None,
        "last": events[-1]["id"] if events else None,
        "events": events,
    }


def export_rowid(store: Store, row: sqlite3.Row) -> int:
    export_row = store.row("SELECT rowid FROM exports WHERE id = ?", (row["id"],))
    if export_row is None:
        raise TriError("path_not_found", "Export not found.", object=row["id"])
    return int(export_row["rowid"])


def export_artifact_fingerprint(store: Store, artifact_id: str | None) -> str | None:
    if not artifact_id:
        return None
    artifact = store.get_artifact(artifact_id)
    return f"sha256:{sha256(Path(artifact['path']).read_bytes())}"


def export_input_fingerprint(store: Store, row: sqlite3.Row) -> str:
    cs = store.get_changeset(row["changeset_id"])
    payload = {
        "changeset": cs["id"],
        "export_kind": row["export_kind"],
        "base_snapshot": cs["base_snapshot_id"],
        "head_snapshot": cs["head_snapshot_id"],
        "hidden_paths": loads(cs["hidden_paths_json"], []),
        "touched_paths": loads(cs["touched_paths_json"], []),
        "target_remote": row["target_remote"],
        "target_ref": row["target_ref"],
    }
    return f"sha256:{sha256(dumps(payload).encode())}"


def export_determinism_data(store: Store, row: sqlite3.Row) -> dict[str, Any]:
    input_fingerprint = export_input_fingerprint(store, row)
    artifact_fingerprint = export_artifact_fingerprint(store, row["artifact_id"])
    rowid = export_rowid(store, row)
    same_input_exports = []
    for prior in store.rows(
        """
        SELECT * FROM exports
        WHERE changeset_id = ? AND export_kind = ? AND rowid < ?
        ORDER BY rowid
        """,
        (row["changeset_id"], row["export_kind"], rowid),
    ):
        if export_input_fingerprint(store, prior) == input_fingerprint:
            same_input_exports.append(prior)
    prior = same_input_exports[-1] if same_input_exports else None
    prior_artifact_fingerprint = export_artifact_fingerprint(store, prior["artifact_id"]) if prior else None
    prior_exported_objects = loads(prior["exported_object_ids_json"], []) if prior else None
    exported_objects = loads(row["exported_object_ids_json"], [])
    same_output: bool | None = None
    if prior:
        if row["export_kind"] == "git_patch":
            same_output = artifact_fingerprint is not None and artifact_fingerprint == prior_artifact_fingerprint
        else:
            same_output = row["status"] == prior["status"] and exported_objects == prior_exported_objects

    diagnostics = loads(row["diagnostics_json"], [])
    decision = loads(row["policy_decision_json"], {})
    reason_code = "first_export"
    explanation = "No earlier export used the same stable input fingerprint."
    deterministic = False
    if row["export_kind"] == "git_patch":
        if row["status"] == "exported":
            deterministic = same_output is not False
            reason_code = "same_patch_content" if prior else "content_addressed_patch"
            explanation = "Git patch export is deterministic for the same changeset base/head and hidden-path policy because the patch content is content-addressed."
        else:
            reason_code = diagnostics[0]["code"] if diagnostics else decision.get("reason_code", "export_failed")
            explanation = "Patch export did not produce content; the diagnostics and policy decision explain why."
    elif row["export_kind"] == "git_commit":
        reason_code = "git_commit_external_state"
        explanation = "Git commit export projects Tri content into an external repository, so commit ids depend on target repository state and Git commit metadata such as parent, author, and timestamp."
    elif row["export_kind"] == "github_pr":
        reason_code = "github_pr_external_state"
        explanation = "GitHub PR export depends on external branch and forge state, so repeated exports may fail or produce different external ids even for the same changeset."
    else:
        reason_code = decision.get("reason_code", "unknown_export_kind")
        explanation = "Tri does not have deterministic re-export rules for this export kind."

    if row["status"] == "policy_blocked":
        reason_code = diagnostics[0]["code"] if diagnostics else decision.get("reason_code", "policy_blocked")
        explanation = "Re-export is blocked by recorded policy diagnostics; no external output is expected."

    return {
        "input_fingerprint": input_fingerprint,
        "artifact_fingerprint": artifact_fingerprint,
        "repeat_of": prior["id"] if prior else None,
        "prior_same_input_count": len(same_input_exports),
        "same_output_as_repeat": same_output,
        "deterministic": deterministic,
        "reason_code": reason_code,
        "explanation": explanation,
    }


def export_data(store: Store, row: sqlite3.Row, *, actor: str | None = None, include_source_events: bool = True) -> dict[str, Any]:
    cs = store.get_changeset(row["changeset_id"])
    workspace_id, redacted_workspace = workspace_ref_for_actor(store, cs["workspace_id"], actor=actor)
    policy_decision = loads(row["policy_decision_json"], {})
    diagnostics = loads(row["diagnostics_json"], [])
    if actor is not None:
        redacted_refs = [0]
        policy_decision = redact_policy_payload_refs(store, policy_decision, actor=actor, counter=redacted_refs)
        diagnostics = redact_policy_payload_refs(store, diagnostics, actor=actor, counter=redacted_refs)
    return {
        "id": row["id"],
        "status": row["status"],
        "project": cs["project_id"],
        "changeset": row["changeset_id"],
        "workspace": workspace_id,
        "redacted_workspace": redacted_workspace,
        "export_kind": row["export_kind"],
        "target_remote": row["target_remote"],
        "target_ref": row["target_ref"],
        "exported_object_ids": loads(row["exported_object_ids_json"], []),
        "policy_decision": policy_decision,
        "actor": row["actor"],
        "artifact": row["artifact_id"],
        "diagnostics": diagnostics,
        "source_events": export_source_events(store, row, actor=actor, include_events=include_source_events),
        "determinism": export_determinism_data(store, row),
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def export_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    source_events = record.get("source_events") if isinstance(record.get("source_events"), dict) else {}
    determinism = record.get("determinism") if isinstance(record.get("determinism"), dict) else {}
    policy_decision = record.get("policy_decision") if isinstance(record.get("policy_decision"), dict) else {}
    return {
        "id": record.get("id"),
        "status": record.get("status"),
        "changeset": record.get("changeset"),
        "export_kind": record.get("export_kind"),
        "artifact": record.get("artifact"),
        "diagnostics": record.get("diagnostics", []),
        "policy": {
            "reason_code": policy_decision.get("reason_code"),
        },
        "source_events": {
            "count": source_events.get("count", 0),
            "last": source_events.get("last"),
        },
        "determinism": {
            "deterministic": determinism.get("deterministic"),
            "reason_code": determinism.get("reason_code"),
            "artifact_fingerprint": determinism.get("artifact_fingerprint"),
        },
    }


def attach_export_record(store: Store, data: dict[str, Any], export_id: str, *, actor: str | None = None, summary: bool = False) -> dict[str, Any]:
    record = export_data(store, store.get_export(export_id), actor=actor, include_source_events=not summary)
    if summary:
        return {**data, "record": export_record_summary(record), "source_events": export_record_summary(record)["source_events"]}
    return {**data, "record": record, "source_events": record["source_events"]}


def mark_changeset_exported(store: Store, cs: sqlite3.Row, *, export_id: str, export_kind: str, actor: str) -> str | None:
    row = store.get_changeset(cs["id"])
    if row["status"] in ("merged", "discarded", "superseded"):
        return None
    previous_status = row["status"]
    lifecycle = loads(row["lifecycle_json"], {})
    exports = lifecycle.get("exports", [])
    if not isinstance(exports, list):
        exports = []
    exports.append({"export": export_id, "kind": export_kind, "actor": actor, "created_at": utcnow()})
    lifecycle = {
        **lifecycle,
        "previous_status": previous_status,
        "status": "exported",
        "transition": f"{previous_status}->exported",
        "exports": exports,
        "last_export": export_id,
        "last_export_kind": export_kind,
        "actor": actor,
        "updated_at": utcnow(),
    }
    store.conn.execute(
        "UPDATE changesets SET status = ?, export_status = ?, lifecycle_json = ? WHERE id = ?",
        ("exported", "exported", dumps(lifecycle), row["id"]),
    )
    return store.create_event(
        kind="changeset.exported",
        actor=actor,
        project_id=row["project_id"],
        workspace_id=row["workspace_id"],
        object_refs={"changeset": row["id"], "export": export_id, "export_kind": export_kind, "previous_status": previous_status, "status": "exported"},
    )


def cmd_export_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    clauses = ["changesets.project_id = ?"]
    params: list[Any] = [project["id"]]
    if args.changeset:
        clauses.append("exports.changeset_id = ?")
        params.append(args.changeset)
    if args.kind:
        clauses.append("exports.export_kind = ?")
        params.append(args.kind)
    where = " AND ".join(clauses)
    limit = max(1, min(args.limit, 500))
    rows = store.rows(
        f"""
        SELECT exports.* FROM exports
        JOIN changesets ON exports.changeset_id = changesets.id
        WHERE {where}
        ORDER BY exports.created_at DESC, exports.rowid DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    exports = []
    redacted = 0
    for row in rows:
        decision, _ = object_access_decision(store, row["changeset_id"], actor=args.actor, action="changeset.read")
        if not decision["allowed"]:
            redacted += 1
            continue
        exports.append(export_data(store, row, actor=args.actor))
    return envelope("tri.export.list", command, actor=args.actor, start=start, data={"project": project["id"], "exports": exports, "redacted": redacted})


def cmd_export_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = store.get_export(args.export)
    require_object_access(store, row["changeset_id"], actor=args.actor, action="changeset.read")
    return envelope("tri.export.inspect", command, actor=args.actor, start=start, data=export_data(store, row, actor=args.actor))


def cmd_export_patch(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    decision, diagnostics = evaluate_export_policy(store, cs, actor=args.actor, action="export.git_patch", override_reason=args.override_reason)
    policy_event = store.record_policy_decision(actor=args.actor, action="export.git_patch", object_id=cs["id"], decision=decision, project_id=cs["project_id"])
    if diagnostics:
        export_id = new_id("export")
        data = {"export": export_id, "changeset": cs["id"]}
        with store.conn:
            store.conn.execute(
                "INSERT INTO exports (id, status, changeset_id, export_kind, target_remote, target_ref, exported_object_ids_json, policy_decision_json, actor, artifact_id, diagnostics_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (export_id, "policy_blocked", cs["id"], "git_patch", None, None, dumps([]), dumps(decision), args.actor, None, dumps(diagnostics), utcnow(), utcnow()),
            )
            store.conn.execute("UPDATE changesets SET export_status = ? WHERE id = ?", ("policy_blocked", cs["id"]))
            evt = store.create_event(
                kind="export.git_patch",
                actor=args.actor,
                project_id=cs["project_id"],
                object_refs={"export": export_id, "changeset": cs["id"], "artifact": None},
                policy_result=decision,
            )
            data = attach_export_record(store, data, export_id, actor=args.actor, summary=getattr(args, "summary", False))
        return envelope("tri.export.git_patch", command, actor=args.actor, start=start, ok=False, events=[policy_event, evt], diagnostics=diagnostics, data=data)
    decision = {
        **decision,
        "action": "export.git_patch",
    }
    export_id = new_id("export")
    artifact_id = None
    status = "exported"
    try:
        patch = patch_for_snapshots(store, cs["base_snapshot_id"], cs["head_snapshot_id"], effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=args.actor))
        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(patch, newline="\n")
            artifact_id = write_artifact(store, "git_patch", f"{export_id}.patch", patch.encode(), labels=[])
            data: dict[str, Any] = {"export": export_id, "changeset": cs["id"], "output": str(out), "artifact": artifact_id}
        else:
            artifact_id = write_artifact(store, "git_patch", f"{export_id}.patch", patch.encode(), labels=[])
            data = {"export": export_id, "changeset": cs["id"], "patch": patch, "artifact": artifact_id}
    except TriError as exc:
        decision["allowed"] = False
        decision["denials"] = [exc.diagnostic["code"]]
        decision["reason_code"] = exc.diagnostic["code"]
        diagnostics = [exc.diagnostic]
        status = "policy_blocked" if exc.diagnostic["code"] in ("secret_export_blocked", "visibility_export_blocked") else "failed"
        data = {"export": export_id, "changeset": cs["id"]}
    with store.conn:
        store.conn.execute(
            "INSERT INTO exports (id, status, changeset_id, export_kind, target_remote, target_ref, exported_object_ids_json, policy_decision_json, actor, artifact_id, diagnostics_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (export_id, status, cs["id"], "git_patch", None, None, dumps([artifact_id] if artifact_id else []), dumps(decision), args.actor, artifact_id, dumps(diagnostics), utcnow(), utcnow()),
        )
        store.conn.execute("UPDATE changesets SET export_status = ? WHERE id = ?", (status, cs["id"]))
        evt = store.create_event(
            kind="export.git_patch",
            actor=args.actor,
            project_id=cs["project_id"],
            object_refs={"export": export_id, "changeset": cs["id"], "artifact": artifact_id},
            policy_result=decision,
        )
        lifecycle_event = mark_changeset_exported(store, cs, export_id=export_id, export_kind="git_patch", actor=args.actor) if status == "exported" else None
        data = attach_export_record(store, data, export_id, actor=args.actor, summary=getattr(args, "summary", False))
    if diagnostics:
        return envelope("tri.export.git_patch", command, actor=args.actor, start=start, ok=False, events=[policy_event, evt], diagnostics=diagnostics, data=data)
    return envelope("tri.export.git_patch", command, actor=args.actor, start=start, events=[event for event in [policy_event, evt, lifecycle_event] if event], data=data)


def cmd_export_commit(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    project = store.get_project(cs["project_id"])
    repo = Path(args.repo or project["root_path"]).resolve()
    decision, diagnostics = evaluate_export_policy(store, cs, actor=args.actor, action="export.git_commit", override_reason=args.override_reason)
    policy_event = store.record_policy_decision(actor=args.actor, action="export.git_commit", object_id=cs["id"], decision=decision, project_id=cs["project_id"])
    export_id = new_id("export")
    artifact_id = None
    commit_id: str | None = None
    status = "exported"
    data: dict[str, Any] = {"export": export_id, "changeset": cs["id"], "repo": str(repo)}
    if diagnostics:
        status = "policy_blocked"
    else:
        try:
            if not (repo / ".git").exists():
                raise TriError("path_not_found", "Git target repository not found.", object=str(repo))
            dirty = run_git(repo, ["status", "--short"], text=True).strip()
            if dirty:
                raise TriError("git_export_failed", "Target Git repository must be clean before export.", object=str(repo))
            patch = patch_for_snapshots(store, cs["base_snapshot_id"], cs["head_snapshot_id"], effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=args.actor))
            artifact_id = write_artifact(store, "git_patch", f"{export_id}.patch", patch.encode(), labels=[])
            patch_path = store.artifacts / f"{export_id}-apply.patch"
            patch_path.write_text(patch, newline="\n")
            run_git(repo, ["apply", "--check", str(patch_path)], text=True)
            run_git(repo, ["apply", str(patch_path)], text=True)
            run_git(repo, ["add", "-A"], text=True)
            run_git(repo, ["commit", "-m", args.message or cs["title"]], text=True)
            commit_id = run_git(repo, ["rev-parse", "HEAD"], text=True).strip()
            data["commit"] = commit_id
            data["artifact"] = artifact_id
        except TriError as exc:
            decision["allowed"] = False
            decision["denials"] = [exc.diagnostic["code"]]
            decision["reason_code"] = exc.diagnostic["code"]
            diagnostics = [exc.diagnostic]
            status = "policy_blocked" if exc.diagnostic["code"] in ("secret_export_blocked", "visibility_export_blocked") else "failed"
    with store.conn:
        store.conn.execute(
            "INSERT INTO exports (id, status, changeset_id, export_kind, target_remote, target_ref, exported_object_ids_json, policy_decision_json, actor, artifact_id, diagnostics_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (export_id, status, cs["id"], "git_commit", str(repo), None, dumps([commit_id] if commit_id else []), dumps(decision), args.actor, artifact_id, dumps(diagnostics), utcnow(), utcnow()),
        )
        store.conn.execute("UPDATE changesets SET export_status = ? WHERE id = ?", (status, cs["id"]))
        evt = store.create_event(
            kind="export.git_commit",
            actor=args.actor,
            project_id=cs["project_id"],
            object_refs={"export": export_id, "changeset": cs["id"], "artifact": artifact_id, "commit": commit_id},
            policy_result=decision,
        )
        lifecycle_event = mark_changeset_exported(store, cs, export_id=export_id, export_kind="git_commit", actor=args.actor) if status == "exported" else None
        data = attach_export_record(store, data, export_id, actor=args.actor)
    if diagnostics:
        return envelope("tri.export.git_commit", command, actor=args.actor, start=start, ok=False, events=[policy_event, evt], diagnostics=diagnostics, data=data)
    return envelope("tri.export.git_commit", command, actor=args.actor, start=start, events=[event for event in [policy_event, evt, lifecycle_event] if event], data=data)


def github_pr_body(store: Store, cs: sqlite3.Row, body: str | None, *, actor: str) -> str:
    if body:
        return body
    data = changeset_data(store, cs["id"], actor=actor)
    lines = [
        f"Tri changeset: {cs['id']}",
        "",
        f"Title: {cs['title']}",
        f"Workspace: {data['workspace']}",
        f"Base snapshot: {data['base_snapshot']}",
        f"Head snapshot: {data['head_snapshot']}",
        "",
        "Touched paths:",
    ]
    lines.extend(f"- {path}" for path in data["touched_paths"])
    return "\n".join(lines) + "\n"


def cmd_export_github_pr(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    cs = store.get_changeset(args.changeset)
    repo = Path(args.repo).resolve()
    decision, diagnostics = evaluate_export_policy(store, cs, actor=args.actor, action="export.github_pr", override_reason=args.override_reason)
    policy_event = store.record_policy_decision(actor=args.actor, action="export.github_pr", object_id=cs["id"], decision=decision, project_id=cs["project_id"])
    export_id = new_id("export")
    artifact_id = None
    patch_artifact: str | None = None
    commit_id: str | None = None
    pr_url: str | None = None
    status = "exported"
    title = args.title or cs["title"]
    body = github_pr_body(store, cs, args.body, actor=args.actor)
    base_ref = args.base or git_default_ref(repo)
    data: dict[str, Any] = {
        "export": export_id,
        "changeset": cs["id"],
        "repo": str(repo),
        "base": base_ref,
        "branch": args.branch,
        "title": title,
    }
    if diagnostics:
        status = "policy_blocked"
    else:
        try:
            ensure_clean_git_repo(repo)
            run_git(repo, ["check-ref-format", "--branch", args.branch], text=True)
            if git_branch_exists(repo, args.branch):
                raise TriError("git_export_failed", "Target branch already exists.", object=args.branch)
            original_ref = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
            try:
                run_git(repo, ["checkout", "-b", args.branch, base_ref], text=True)
                patch = patch_for_snapshots(store, cs["base_snapshot_id"], cs["head_snapshot_id"], effective_hidden_patterns(store, cs["project_id"], loads(cs["hidden_paths_json"], []), actor=args.actor))
                patch_artifact = write_artifact(store, "git_patch", f"{export_id}.patch", patch.encode(), labels=[])
                patch_path = store.artifacts / f"{export_id}-apply.patch"
                patch_path.write_text(patch, newline="\n")
                run_git(repo, ["apply", "--check", str(patch_path)], text=True)
                run_git(repo, ["apply", str(patch_path)], text=True)
                run_git(repo, ["add", "-A"], text=True)
                run_git(repo, ["commit", "-m", title], text=True)
                commit_id = run_git(repo, ["rev-parse", "HEAD"], text=True).strip()
                request_payload = {
                    "kind": "github_pr_request",
                    "changeset": cs["id"],
                    "repo": str(repo),
                    "base": base_ref,
                    "branch": args.branch,
                    "title": title,
                    "body": body,
                    "commit": commit_id,
                    "draft": args.draft,
                    "patch_artifact": patch_artifact,
                }
                if args.create:
                    if shutil.which("gh") is None:
                        raise TriError("git_export_failed", "GitHub CLI `gh` is required for --create.", object="gh")
                    gh_cmd = ["gh", "pr", "create", "--base", base_ref, "--head", args.branch, "--title", title, "--body", body]
                    if args.draft:
                        gh_cmd.append("--draft")
                    result = subprocess.run(gh_cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                    if result.returncode != 0:
                        raise TriError("git_export_failed", result.stderr.strip() or "GitHub PR creation failed.", object=args.branch)
                    pr_url = result.stdout.strip()
                    request_payload["url"] = pr_url
                artifact_id = write_artifact(store, "github_pr_request", f"{export_id}-github-pr.json", dumps(request_payload).encode(), labels=[])
                data.update({"commit": commit_id, "artifact": artifact_id, "patch_artifact": patch_artifact, "url": pr_url})
            except TriError:
                if original_ref:
                    subprocess.run(["git", "-C", str(repo), "checkout", original_ref], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                raise
        except TriError as exc:
            decision["allowed"] = False
            decision["denials"] = [exc.diagnostic["code"]]
            decision["reason_code"] = exc.diagnostic["code"]
            diagnostics = [exc.diagnostic]
            status = "policy_blocked" if exc.diagnostic["code"] in ("secret_export_blocked", "visibility_export_blocked") else "failed"
    with store.conn:
        exported_object_ids = [item for item in [commit_id, pr_url, patch_artifact] if item]
        store.conn.execute(
            "INSERT INTO exports (id, status, changeset_id, export_kind, target_remote, target_ref, exported_object_ids_json, policy_decision_json, actor, artifact_id, diagnostics_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (export_id, status, cs["id"], "github_pr", str(repo), args.branch, dumps(exported_object_ids), dumps(decision), args.actor, artifact_id, dumps(diagnostics), utcnow(), utcnow()),
        )
        store.conn.execute("UPDATE changesets SET export_status = ? WHERE id = ?", (status, cs["id"]))
        evt = store.create_event(
            kind="export.github_pr",
            actor=args.actor,
            project_id=cs["project_id"],
            object_refs={"export": export_id, "changeset": cs["id"], "artifact": artifact_id, "patch_artifact": patch_artifact, "commit": commit_id, "branch": args.branch, "url": pr_url},
            policy_result=decision,
        )
        lifecycle_event = mark_changeset_exported(store, cs, export_id=export_id, export_kind="github_pr", actor=args.actor) if status == "exported" else None
        data = attach_export_record(store, data, export_id, actor=args.actor)
    if diagnostics:
        return envelope("tri.export.github_pr", command, actor=args.actor, start=start, ok=False, events=[policy_event, evt], diagnostics=diagnostics, data=data)
    return envelope("tri.export.github_pr", command, actor=args.actor, start=start, events=[event for event in [policy_event, evt, lifecycle_event] if event], data=data)


def cmd_config_materialize(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    content = f"TRI_PROFILE={args.profile}\nTRI_WORKSPACE={ws['id']}\n".encode()
    config_path = generated_config_path(args.profile)
    artifact = write_artifact(store, "generated_config", f"{ws['id']}-{args.profile}.env", content, labels=["non-exportable"])
    active_lease = implicit_materialization_lease(store, ws["id"], actor=args.actor)
    lease_id = None
    materialized_path = None
    with store.conn:
        if active_lease:
            require_active_materialization_lease(store, active_lease)
            lease_id = active_lease["id"]
            root = Path(active_lease["path"])
            if not root.exists():
                raise TriError("stale_materialization", "Materialized path no longer exists.", object=str(root), lease=lease_id)
            materialized_path = str(write_materialized_config(root, config_path, content))
            generated_artifacts = loads(active_lease["generated_artifacts_json"], [])
            if artifact not in generated_artifacts:
                generated_artifacts.append(artifact)
            ignored_paths = loads(active_lease["ignored_paths_json"], [])
            if config_path not in ignored_paths:
                ignored_paths.append(config_path)
            store.conn.execute(
                "UPDATE materialization_leases SET generated_artifacts_json = ?, ignored_paths_json = ? WHERE id = ?",
                (dumps(generated_artifacts), dumps(sorted(set(ignored_paths))), active_lease["id"]),
            )
        evt = store.create_event(
            kind="config.materialized",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"artifact": artifact, "profile": args.profile, "lease": lease_id, "path": config_path},
            policy_result={"allowed": True, "labels": ["non-exportable"]},
        )
    return envelope(
        "tri.config.materialize",
        command,
        actor=args.actor,
        start=start,
        events=[evt],
        data={
            "workspace": ws["id"],
            "profile": args.profile,
            "artifact": artifact,
            "labels": ["non-exportable"],
            "lease": lease_id,
            "path": config_path,
            "materialized_path": materialized_path,
        },
    )


def cmd_secrets_put(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can create project secrets.", object=args.project, required_grant="role:maintainer")
    project = store.get_project(args.project)
    existing = store.row("SELECT id FROM secret_refs WHERE project_id = ? AND name = ?", (project["id"], args.name))
    if existing is not None:
        raise TriError("policy_denied", "Secret already exists; revoke and create a new name for rotation in this MVP.", object=args.name)
    value = Path(args.value_file).read_bytes()
    secret_id = new_id("secret")
    secret_path = store.secrets / secret_id
    secret_path.write_bytes(value)
    fingerprint = f"sha256:{sha256(value)}"
    with store.conn:
        store.conn.execute(
            "INSERT INTO secret_refs (id, project_id, name, provider, value_path, fingerprint, scope_json, created_by, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (secret_id, project["id"], args.name, "local_file", str(secret_path), fingerprint, dumps(args.scope or ["workspace"]), args.actor, utcnow(), None),
        )
        evt = store.create_event(
            kind="secret.created",
            actor=args.actor,
            project_id=project["id"],
            object_refs={"secret": secret_id, "name": args.name, "fingerprint": fingerprint},
            policy_result={"allowed": True, "labels": ["secret-ref-only"]},
        )
    return envelope("tri.secrets.inspect", command, actor=args.actor, start=start, events=[evt], data=secret_data(store.get_secret(secret_id)))


def cmd_secrets_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    rows = store.rows("SELECT * FROM secret_refs WHERE project_id = ? ORDER BY name", (project["id"],))
    secrets = []
    redacted = 0
    for row in rows:
        decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="secret.read")
        if decision["allowed"]:
            secrets.append(secret_data(row))
        else:
            redacted += 1
    return envelope("tri.secrets.list", command, actor=args.actor, start=start, data={"project": project["id"], "secrets": secrets, "redacted": redacted})


def resolve_secret_for_inspect(store: Store, secret_ref: str, project_ref: str | None = None) -> sqlite3.Row:
    if project_ref:
        project = store.get_project(project_ref)
        row = store.row(
            "SELECT * FROM secret_refs WHERE project_id = ? AND (id = ? OR name = ?)",
            (project["id"], secret_ref, secret_ref),
        )
        if row is None:
            raise TriError("path_not_found", "Secret not found for project.", object=secret_ref, project=project["id"])
        return row
    return store.get_secret(secret_ref)


def cmd_secrets_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    secret = resolve_secret_for_inspect(store, args.secret, args.project)
    require_object_access(store, secret["id"], actor=args.actor, action="secret.read")
    return envelope("tri.secrets.inspect", command, actor=args.actor, start=start, data=secret_data(secret))


def cmd_secrets_request_lease(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    ws = store.get_workspace(args.workspace)
    if ws["status"] != "active":
        raise TriError("policy_denied", "Only active workspaces can request secret leases.", object=ws["id"])
    require_object_access(store, ws["id"], actor=args.actor, action="workspace.write")
    secret = resolve_secret_for_workspace(store, ws, args.secret)
    require_object_access(store, secret["id"], actor=args.actor, action="secret.use")
    if secret["revoked_at"]:
        raise TriError("policy_denied", "Secret has been revoked.", object=secret["id"])
    lease_id = new_id("lease")
    expires_at = None
    if args.ttl_seconds:
        expires_at = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=args.ttl_seconds)).isoformat().replace("+00:00", "Z")
    with store.conn:
        store.conn.execute(
            "INSERT INTO secret_leases (id, secret_id, workspace_id, actor, env_name, status, expires_at, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lease_id, secret["id"], ws["id"], args.actor, args.env, "active", expires_at, utcnow(), None),
        )
        evt = store.create_event(
            kind="secret.lease_granted",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"secret": secret["id"], "lease": lease_id, "env": args.env},
            policy_result={"allowed": True, "scope": "workspace", "expires_at": expires_at},
        )
    return envelope("tri.secret_lease.inspect", command, actor=args.actor, start=start, events=[evt], data=secret_lease_data(store.get_secret_lease(lease_id)))


def cmd_secrets_revoke(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    lease = store.get_secret_lease(args.lease)
    if lease["actor"] != args.actor:
        require_object_access(store, lease["workspace_id"], actor=args.actor, action="workspace.write")
    status = "revoked" if lease["status"] != "revoked" else lease["status"]
    with store.conn:
        store.conn.execute("UPDATE secret_leases SET status = ?, revoked_at = ? WHERE id = ?", (status, utcnow(), lease["id"]))
        ws = store.get_workspace(lease["workspace_id"])
        evt = store.create_event(
            kind="secret.lease_revoked",
            actor=args.actor,
            project_id=ws["project_id"],
            workspace_id=ws["id"],
            task_id=ws["task_id"],
            object_refs={"secret": lease["secret_id"], "lease": lease["id"]},
            policy_result={"allowed": True},
        )
    return envelope("tri.secret_lease.inspect", command, actor=args.actor, start=start, events=[evt], data=secret_lease_data(store.get_secret_lease(args.lease)))


def require_check_log_redaction_access(store: Store, check: dict[str, Any], *, actor: str) -> None:
    if check["changeset"]:
        require_object_access(store, check["changeset"], actor=actor, action="changeset.write")
    elif check["workspace"]:
        require_object_access(store, check["workspace"], actor=actor, action="workspace.write")
    elif "role:maintainer" not in actor_selectors(actor):
        raise TriError("policy_denied", "Only maintainers can redact unscoped check logs.", object=check["id"])


def cmd_secrets_redact_logs(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    check = check_data(store, args.check)
    require_check_log_redaction_access(store, check, actor=args.actor)
    lease_ids = sorted(set([*(check["secret_leases"] or []), *(args.lease or [])]))
    redactions = secret_redactions_for_leases(store, lease_ids, workspace_id=check["workspace"])
    log_artifacts = [artifact_id for artifact_id in check["logs"].values() if artifact_id]
    redacted_artifacts: list[dict[str, Any]] = []
    replacement_count = 0
    with store.conn:
        for artifact_id in log_artifacts:
            artifact = store.get_artifact(artifact_id)
            path = Path(artifact["path"])
            before = path.read_bytes()
            replacements = sum(before.count(value) for _, value in redactions if value)
            after = redact_secret_bytes(before, redactions)
            labels = loads(artifact["labels_json"], [])
            old_labels = list(labels)
            if redactions and "secret-redacted" not in labels:
                labels.append("secret-redacted")
            if after != before or labels != old_labels:
                path.write_bytes(after)
                store.conn.execute("UPDATE artifacts SET size = ?, labels_json = ? WHERE id = ?", (len(after), dumps(labels), artifact_id))
            replacement_count += replacements
            if replacements:
                redacted_artifacts.append({"artifact": artifact_id, "replacements": replacements, "size": len(after)})
        project_id = None
        task_id = None
        if check["workspace"]:
            ws = store.get_workspace(check["workspace"])
            project_id = ws["project_id"]
            task_id = ws["task_id"]
        elif check["changeset"]:
            cs = store.get_changeset(check["changeset"])
            project_id = cs["project_id"]
        evt = store.create_event(
            kind="secret.logs_redacted",
            actor=args.actor,
            project_id=project_id,
            workspace_id=check["workspace"],
            task_id=task_id,
            object_refs={"check": check["id"], "artifacts": log_artifacts, "redacted_artifacts": redacted_artifacts, "leases": lease_ids},
            policy_result={"allowed": True, "replacement_count": replacement_count},
        )
    data = {
        "check": check["id"],
        "leases": lease_ids,
        "artifacts": log_artifacts,
        "redacted_artifacts": redacted_artifacts,
        "replacement_count": replacement_count,
    }
    return envelope("tri.secrets.redact_logs", command, actor=args.actor, start=start, events=[evt], data=data)


def cmd_secrets_audit(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    clauses = ["kind LIKE 'secret.%'"]
    params: list[Any] = []
    if args.workspace:
        clauses.append("workspace_id = ?")
        params.append(args.workspace)
    if args.project:
        project = store.get_project(args.project)
        clauses.append("project_id = ?")
        params.append(project["id"])
    if args.secret:
        clauses.append("object_refs_json LIKE ?")
        params.append(f"%{args.secret}%")
    where = f"WHERE {' AND '.join(clauses)}"
    rows = store.rows(f"SELECT * FROM events {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params, max(1, min(args.limit, 500))))
    events = []
    redacted = 0
    for row in rows:
        try:
            events.append(event_view(store, row, actor=args.actor, record_denial=False))
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return envelope("tri.secrets.audit", command, actor=args.actor, start=start, data={"events": events, "redacted": redacted})


def cmd_commands(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    commands = [{**contract, "schema_version": SCHEMA_VERSION} for contract in COMMAND_CONTRACTS]
    return envelope("tri.commands", command, actor=args.actor, start=start, data={"schema_version": SCHEMA_VERSION, "commands": commands})


def cmd_schema(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    def schema_document(name: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "schema_version": SCHEMA_VERSION, **copy.deepcopy(schema)}

    if args.name:
        schema = SCHEMA_CATALOG.get(args.name)
        if schema is None:
            raise TriError("path_not_found", "Schema not found.", object=args.name)
        data = {"schema_version": SCHEMA_VERSION, "name": args.name, "schema": schema_document(args.name, schema)}
    else:
        data = {"schema_version": SCHEMA_VERSION, "schemas": {name: schema_document(name, schema) for name, schema in SCHEMA_CATALOG.items()}}
    return envelope("tri.schema", command, actor=args.actor, start=start, data=data)


def cmd_batch(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    if args.plan == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.plan).read_text()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise TriError("invalid_command", f"Invalid batch JSON: {exc.msg}")
    if not isinstance(payload, dict):
        raise TriError("invalid_command", "Batch plan must be a JSON object.")
    if args.continue_on_error:
        payload = {**payload, "continue_on_error": True}
    data, ok = run_batch_payload(store, payload, default_actor=args.actor, start=start)
    return envelope("tri.batch", command, actor=args.actor, start=start, ok=ok, data=data)


def cmd_actors_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.actor.inspect", command, actor=args.actor, start=start, data=actor_identity_data(args.identity))


def cmd_doctor(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    counts = {
        name: store.row(f"SELECT COUNT(*) AS count FROM {name}", ())["count"]
        for name in [
            "projects",
            "targets",
            "blobs",
            "trees",
            "snapshots",
            "workspaces",
            "operations",
            "events",
            "tasks",
            "task_runs",
            "check_runs",
            "check_sets",
            "package_boundaries",
            "changesets",
            "reviews",
            "review_actions",
            "artifacts",
            "exports",
            "workspace_conflicts",
            "secret_refs",
            "secret_leases",
            "materialization_leases",
            "policy_grants",
            "advisories",
            "releases",
        ]
    }
    warnings = []
    if shutil.which("git") is None:
        warnings.append({"code": "git_missing", "severity": "warning", "message": "Git is not available; import/export commands will fail.", "retryable": False})
    materialization = materialization_status(store)
    if materialization["store_volume"]["backend"] == "copy":
        warnings.append(
            {
                "code": "materialization_degraded",
                "severity": "warning",
                "message": "This volume cannot share file blocks, so run and mount directories are full copies. Disk use is still bounded by live leases.",
                "retryable": False,
            }
        )
    data = {
        "home": str(store.home),
        "db": str(store.db_path),
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "git_available": shutil.which("git") is not None,
        "sandbox": sandbox.probe(),
        "command_count": len(COMMAND_CONTRACTS),
        "materialization": materialization,
    }
    return envelope("tri.doctor", command, actor=args.actor, start=start, data=data, warnings=warnings)


def directory_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for child in path.iterdir() if not child.name.startswith("."))


def materialization_status(store: Store) -> dict[str, Any]:
    store.checkout.mkdir(parents=True, exist_ok=True)
    override = os.environ.get("TRI_MATERIALIZE_BACKEND") or None
    method = None if (override or "").lower() == "copy" else probe_clone(store.checkout)
    return {
        "store_volume": {
            "backend": "clone" if method else "copy",
            "clone_method": method,
            "dir_clone": bool(method) and clone_dir_supported(),
            "overlay": probe_overlay(store.runs),
        },
        "backend_override": override,
        "caches": {
            "checkout_cache_bytes": store.materializer.cache.size_bytes(),
            "base_dirs": directory_count(store.home / "bases"),
        },
        "leases": lease_status_summary(store),
    }


def cmd_ignore_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    project = store.get_project(args.project)
    return envelope("tri.ignore.list", command, actor=args.actor, start=start, data={"project": project["id"], "patterns": project_ignore_patterns(store, project["id"])})


def _require_ignore_maintainer(args: argparse.Namespace) -> None:
    if "role:maintainer" not in actor_selectors(args.actor):
        raise TriError("policy_denied", "Only maintainers can change project ignore patterns.", object=args.project, action="project.ignore", required_grant="role:maintainer")


def cmd_ignore_add(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    _require_ignore_maintainer(args)
    project = store.get_project(args.project)
    patterns = sorted({pattern.strip() for pattern in args.patterns if pattern.strip()})
    if not patterns:
        raise TriError("invalid_command", "At least one non-empty ignore pattern is required.", object=args.project)
    with store.conn:
        for pattern in patterns:
            store.conn.execute(
                "INSERT OR IGNORE INTO project_ignores (project_id, pattern, created_by, created_at) VALUES (?, ?, ?, ?)",
                (project["id"], pattern, args.actor, utcnow()),
            )
        evt = store.create_event(kind="project.ignore_added", actor=args.actor, project_id=project["id"], object_refs={"patterns": patterns})
    return envelope("tri.ignore.list", command, actor=args.actor, start=start, events=[evt], data={"project": project["id"], "patterns": project_ignore_patterns(store, project["id"])})


def cmd_ignore_remove(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    _require_ignore_maintainer(args)
    project = store.get_project(args.project)
    patterns = sorted({pattern.strip() for pattern in args.patterns if pattern.strip()})
    with store.conn:
        for pattern in patterns:
            store.conn.execute("DELETE FROM project_ignores WHERE project_id = ? AND pattern = ?", (project["id"], pattern))
        evt = store.create_event(kind="project.ignore_removed", actor=args.actor, project_id=project["id"], object_refs={"patterns": patterns})
    return envelope("tri.ignore.list", command, actor=args.actor, start=start, events=[evt], data={"project": project["id"], "patterns": project_ignore_patterns(store, project["id"])})


def cmd_objects_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    object_type, resolved_argv = object_inspect_command(args.object)
    output, _ = execute_argv(resolved_argv, actor=args.actor, start=start, store=store)
    data = {
        "object": args.object,
        "object_type": object_type,
        "resolved_command": output["command"],
        "resolved_schema": output["schema"],
        "result_ok": output["ok"],
        "result": output["data"],
    }
    return envelope(
        "tri.object.inspect",
        command,
        actor=args.actor,
        start=start,
        ok=output["ok"],
        events=output["events"],
        diagnostics=output["diagnostics"],
        warnings=output["warnings"],
        refs=output["refs"],
        data=data,
    )


def cmd_events_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    clauses = []
    params: list[Any] = []
    if args.workspace:
        clauses.append("workspace_id = ?")
        params.append(args.workspace)
    if args.project:
        project = store.get_project(args.project)
        clauses.append("project_id = ?")
        params.append(project["id"])
    if args.kind:
        clauses.append("kind = ?")
        params.append(args.kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(args.limit, 500))
    rows = store.rows(f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?", (*params, limit))
    events = []
    redacted = 0
    for row in rows:
        try:
            events.append(event_view(store, row, actor=args.actor, record_denial=False))
        except TriError as exc:
            if exc.diagnostic["code"] != "policy_denied":
                raise
            redacted += 1
    return envelope("tri.events.list", command, actor=args.actor, start=start, data={"events": events, "redacted": redacted})


def cmd_events_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    row = store.row("SELECT * FROM events WHERE id = ?", (args.event,))
    if row is None:
        raise TriError("path_not_found", "Event not found.", object=args.event)
    return envelope("tri.events.inspect", command, actor=args.actor, start=start, data=event_view(store, row, actor=args.actor))


def cmd_artifacts_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    clauses = []
    params: list[Any] = []
    if args.kind:
        clauses.append("kind = ?")
        params.append(args.kind)
    if args.label:
        clauses.append("labels_json LIKE ?")
        params.append(f"%{args.label}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(args.limit, 500))
    rows = store.rows(f"SELECT * FROM artifacts {where} ORDER BY created_at DESC LIMIT ?", (*params, limit))
    artifacts = []
    redacted = 0
    for row in rows:
        decision, _ = artifact_access_decision(store, row["id"], actor=args.actor)
        if decision["allowed"]:
            data = artifact_data(row)
            data["origins"] = decision["origins"]
            data["access"] = decision
            artifacts.append(data)
        else:
            redacted += 1
    return envelope("tri.artifacts.list", command, actor=args.actor, start=start, data={"artifacts": artifacts, "redacted": redacted})


def cmd_artifacts_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.artifacts.inspect", command, actor=args.actor, start=start, data=artifact_view(store, store.get_artifact(args.artifact), actor=args.actor))


def cmd_artifacts_read(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    return envelope("tri.artifacts.read", command, actor=args.actor, start=start, data=artifact_view(store, store.get_artifact(args.artifact), actor=args.actor, include_text=True))


def review_data(store: Store, review_id: str, *, actor: str = "actor_local") -> dict[str, Any]:
    row = store.get_review(review_id)
    all_actions = [review_action_data(action) for action in store.rows("SELECT * FROM review_actions WHERE review_id = ? ORDER BY created_at, rowid", (review_id,))]
    assignment = review_assignment_state(row, all_actions)
    cs = store.get_changeset(row["changeset_id"])
    changeset_decision, changeset_diagnostics = object_access_decision(store, cs["id"], actor=actor, action="changeset.read")
    can_read_changeset = changeset_decision["allowed"]
    actions = all_actions if can_read_changeset else [redacted_review_action_data(action) for action in all_actions]
    comment_threads = review_comment_threads(actions)
    attached_policy_findings = [action for action in actions if action["kind"] == "policy_finding_attached"]
    attached_check_summaries = [action for action in actions if action["kind"] == "check_summary_attached"]
    check_summary = loads(row["check_summary_json"], {})
    workspace = None
    workspace_redacted = False
    task = None
    task_run = None
    checks: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    if can_read_changeset:
        cs_data = changeset_data(store, cs["id"], actor=actor)
        workspace_id, workspace_redacted = workspace_ref_for_actor(store, cs["workspace_id"], actor=actor)
        workspace = workspace_data(store, workspace_id, actor=actor) if workspace_id else None
        if cs["task_run_id"]:
            run = store.row("SELECT * FROM task_runs WHERE id = ?", (cs["task_run_id"],))
            if run:
                task_run = task_run_view(store, run, actor=actor)
                task_decision, _ = object_access_decision(store, run["task_id"], actor=actor, action="task.read") if run["task_id"] else ({"allowed": False}, [])
                if run["task_id"] and task_decision["allowed"]:
                    task = task_data(store, run["task_id"], actor=actor)
        elif workspace and workspace["task"]:
            task_decision, _ = object_access_decision(store, workspace["task"], actor=actor, action="task.read")
            if task_decision["allowed"]:
                task = task_data(store, workspace["task"], actor=actor)
        checks = [check_view(store, check_id, actor=actor) for check_id in loads(cs["checks_json"], [])]
        artifacts = [
            {"check": check["id"], "artifact": artifact_data(store.get_artifact(artifact_id))}
            for check in checks
            for artifact_id in check["artifacts"]
        ]
        policy_decision, policy_diagnostics = evaluate_export_policy(store, cs, actor=actor, action="review.inspect")
        visibility_impact = {
            "visibility": cs_data["visibility"],
            "hidden_paths": cs_data["hidden_paths"],
            "redacted_touched_paths": cs_data["redacted_touched_paths"],
            "diff_summary": loads(row["export_preview_json"], {}),
            "redacted": False,
        }
        changeset_summary = {
            "id": cs_data["id"],
            "title": cs_data["title"],
            "status": cs_data["status"],
            "base_snapshot": cs_data["base_snapshot"],
            "head_snapshot": cs_data["head_snapshot"],
            "touched_paths": cs_data["touched_paths"],
            "redacted_touched_paths": cs_data["redacted_touched_paths"],
            "suggested_split_points": cs_data["suggested_split_points"],
            "stack": cs_data["stack"],
            "redacted": False,
        }
        redacted_policy_refs = [0]
        policy_decision = redact_policy_payload_refs(store, policy_decision, actor=actor, counter=redacted_policy_refs)
        policy_diagnostics = redact_policy_payload_refs(store, policy_diagnostics, actor=actor, counter=redacted_policy_refs)
    else:
        check_summary = {
            **check_summary,
            "latest_required": [],
            "required_diagnostics": [
                decision_denial(
                    "redacted_changeset",
                    "Backing changeset details are redacted for this review reader.",
                    object=review_id,
                )
            ],
            "redacted": True,
        }
        policy_decision = {
            "id": changeset_decision["id"],
            "allowed": False,
            "actor": actor,
            "action": "changeset.read",
            "object": None,
            "object_type": "changeset",
            "policies": ["local_policy_v0"],
            "grants": [],
            "matched_grants": [],
            "denials": ["policy_denied"],
            "redactions": [{"field": "changeset", "reason": "missing_changeset_read"}],
            "reason_code": "policy_denied",
            "reason": changeset_decision["reason"],
            "event": None,
            "redacted": True,
        }
        policy_diagnostics = [
            decision_denial(
                "policy_denied",
                "Backing changeset details are redacted for this review reader.",
                object=review_id,
                required_grant="changeset.read",
            )
        ]
        visibility_impact = {
            "visibility": ["redacted"],
            "hidden_paths": [],
            "redacted_touched_paths": len(loads(cs["touched_paths_json"], [])),
            "diff_summary": {"redacted": True},
            "redacted": True,
        }
        changeset_summary = {
            "id": None,
            "title": None,
            "status": "redacted",
            "base_snapshot": None,
            "head_snapshot": None,
            "touched_paths": [],
            "redacted_touched_paths": len(loads(cs["touched_paths_json"], [])),
            "suggested_split_points": [],
            "stack": {"redacted": True},
            "redacted": True,
        }
    return {
        "id": row["id"],
        "changeset": row["changeset_id"] if can_read_changeset else None,
        "redacted_changeset": not can_read_changeset,
        "workspace": workspace["id"] if workspace else None,
        "redacted_workspace": workspace_redacted,
        "status": row["status"],
        "reviewers": loads(row["reviewers_json"], []),
        "approvals": assignment["approvals"],
        "approved_reviewers": assignment["approved_reviewers"],
        "pending_reviewers": assignment["pending_reviewers"],
        "maintainer_override": assignment["maintainer_override"],
        "check_summary": check_summary,
        "risk_summary": row["risk_summary"],
        "export_preview": loads(row["export_preview_json"], {}) if can_read_changeset else {"redacted": True},
        "changeset_summary": changeset_summary,
        "task": task,
        "task_run": task_run,
        "actor_identity": {
            "workspace_owner": workspace["owner"] if can_read_changeset and workspace else None,
            "task_actor": task_run["actor"] if task_run else None,
        },
        "commands": [{"check": check["id"], "command": check["command"], "status": check["status"], "assumptions": check["assumptions"]} for check in checks],
        "checks": checks,
        "artifacts": artifacts,
        "policy_findings": {"decision": policy_decision, "diagnostics": policy_diagnostics},
        "attached_policy_findings": attached_policy_findings,
        "attached_check_summaries": attached_check_summaries,
        "visibility_impact": visibility_impact,
        "comment_threads": comment_threads,
        "unresolved_risks": [action for action in actions if action["kind"] == "risk_marked"],
        "actions": actions,
        "created_at": row["created_at"],
    }


def cmd_review_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_object_access(store, args.review, actor=args.actor, action="review.read")
    return envelope("tri.review.inspect", command, actor=args.actor, start=start, data=review_data(store, args.review, actor=args.actor))


def cmd_review_action_inspect(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    action = get_review_action(store, args.action)
    return envelope("tri.review_action.inspect", command, actor=args.actor, start=start, data=review_action_view(store, action, actor=args.actor))


def cmd_task_attempts(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    task = store.get_task(args.task)
    require_object_access(store, task["id"], actor=args.actor, action="task.read")
    workspaces = []
    redacted = 0
    for row in store.rows("SELECT id FROM workspaces WHERE task_id = ? ORDER BY created_at", (args.task,)):
        decision, _ = object_access_decision(store, row["id"], actor=args.actor, action="workspace.read")
        if decision["allowed"]:
            workspaces.append(workspace_data(store, row["id"], actor=args.actor))
        else:
            redacted += 1
    runs = []
    for row in store.rows("SELECT * FROM task_runs WHERE task_id = ? ORDER BY created_at", (args.task,)):
        run = task_run_view(store, row, actor=args.actor)
        if run.get("redacted_workspace"):
            redacted += 1
            continue
        runs.append(run)
    changesets = []
    for workspace in workspaces:
        for cs_id in workspace["changesets"]:
            decision, _ = object_access_decision(store, cs_id, actor=args.actor, action="changeset.read")
            if decision["allowed"]:
                changesets.append(changeset_data(store, cs_id, actor=args.actor))
            else:
                redacted += 1
    return envelope(
        "tri.task.attempts",
        command,
        actor=args.actor,
        start=start,
        data={
            "task": task_data(store, task["id"], actor=args.actor),
            "workspaces": workspaces,
            "runs": runs,
            "changesets": changesets,
            "redacted": redacted,
        },
    )


def cmd_changeset_compare(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    items = []
    redacted = 0
    for cs_id in args.changesets:
        decision, _ = object_access_decision(store, cs_id, actor=args.actor, action="changeset.read")
        if not decision["allowed"]:
            redacted += 1
            continue
        row = store.get_changeset(cs_id)
        cs = changeset_data(store, cs_id, actor=args.actor)
        checks = [check_data(store, check_id) for check_id in cs["checks"]]
        hidden_patterns = effective_hidden_patterns(store, row["project_id"], loads(row["hidden_paths_json"], []), actor=args.actor)
        diff = diff_data(store, cs["base_snapshot"], cs["head_snapshot"], hidden_patterns)
        items.append(
            {
                "changeset": cs_id,
                "title": cs["title"],
                "status": cs["status"],
                "touched_paths": cs["touched_paths"],
                "diff_summary": diff["summary"],
                "checks": {
                    "total": len(checks),
                    "passed": sum(1 for check in checks if check["status"] == "passed"),
                    "failed": sum(1 for check in checks if check["status"] == "failed"),
                },
                "export_status": cs["export_status"],
            }
        )
    return envelope("tri.changeset.compare", command, actor=args.actor, start=start, data={"changesets": items, "redacted": redacted})


class TriHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    authority: TokenAuthority | None = None

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves socket.getfqdn(host), a reverse DNS
        # lookup that can stall for ~30 s on hosts with slow resolvers (seen on
        # macOS CI). The name is only cosmetic, so use the host as given.
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def api_endpoint_catalog(host: str, port: int) -> dict[str, str]:
    base = f"http://{host}:{port}"
    return {
        "root": f"{base}/v0",
        "health": f"{base}/v0/health",
        "commands": f"{base}/v0/commands",
        "schema": f"{base}/v0/schema",
        "schema_named": f"{base}/v0/schema/<schema>",
        "command": f"{base}/v0/command",
        "batch": f"{base}/v0/batch",
        "object_inspect": f"{base}/v0/objects/<object>",
        "actor": f"{base}/v0/actors/<actor>",
        "projects": f"{base}/v0/projects",
        "project": f"{base}/v0/projects/<project>",
        "project_packages": f"{base}/v0/projects/<project>/packages",
        "package": f"{base}/v0/packages/<package>",
        "project_targets": f"{base}/v0/projects/<project>/targets",
        "project_target": f"{base}/v0/projects/<project>/targets/<target-or-id>",
        "target": f"{base}/v0/targets/<target>",
        "project_snapshots": f"{base}/v0/projects/<project>/snapshots",
        "project_workspaces": f"{base}/v0/projects/<project>/workspaces",
        "project_search": f"{base}/v0/projects/<project>/search?q=<query>&scope=workspaces&include_history=true",
        "project_check_sets": f"{base}/v0/projects/<project>/check-sets",
        "check_set": f"{base}/v0/check-sets/<check-set>",
        "project_advisories": f"{base}/v0/projects/<project>/advisories",
        "advisory": f"{base}/v0/advisories/<advisory>",
        "project_releases": f"{base}/v0/projects/<project>/releases",
        "release": f"{base}/v0/releases/<release>",
        "project_exports": f"{base}/v0/projects/<project>/exports?changeset=<changeset>&kind=github_pr",
        "export": f"{base}/v0/exports/<export>",
        "project_secrets": f"{base}/v0/projects/<project>/secrets",
        "secret": f"{base}/v0/secrets/<secret>",
        "project_events": f"{base}/v0/projects/<project>/events?kind=workspace.created&limit=50",
        "task": f"{base}/v0/tasks/<task>",
        "task_run": f"{base}/v0/task-runs/<run>",
        "task_attempts": f"{base}/v0/tasks/<task>/attempts",
        "snapshot": f"{base}/v0/snapshots/<snapshot>",
        "snapshot_files": f"{base}/v0/snapshots/<snapshot>/files",
        "snapshot_file": f"{base}/v0/snapshots/<snapshot>/files/<path>",
        "tree": f"{base}/v0/trees/<tree>",
        "blob": f"{base}/v0/blobs/<blob>",
        "operation": f"{base}/v0/operations/<operation>",
        "workspace": f"{base}/v0/workspaces/<workspace>",
        "workspace_files": f"{base}/v0/workspaces/<workspace>/files",
        "workspace_file": f"{base}/v0/workspaces/<workspace>/files/<path>",
        "workspace_search": f"{base}/v0/workspaces/<workspace>/search?q=<query>&glob=src/**",
        "workspace_diff": f"{base}/v0/workspaces/<workspace>/diff?mode=summary&path=src/**",
        "workspace_operations": f"{base}/v0/workspaces/<workspace>/operations",
        "workspace_checks": f"{base}/v0/workspaces/<workspace>/checks",
        "workspace_conflicts": f"{base}/v0/workspaces/<workspace>/conflicts?status=open",
        "workspace_leases": f"{base}/v0/workspaces/<workspace>/leases",
        "lease": f"{base}/v0/leases/<lease>",
        "changeset": f"{base}/v0/changesets/<changeset>",
        "changeset_operations": f"{base}/v0/changesets/<changeset>/operations",
        "changeset_checks": f"{base}/v0/changesets/<changeset>/checks",
        "changeset_diff": f"{base}/v0/changesets/<changeset>/diff?mode=patch&path=src/app.ts",
        "review": f"{base}/v0/reviews/<review>",
        "review_action": f"{base}/v0/review-actions/<review-action>",
        "check": f"{base}/v0/checks/<check>",
        "check_artifacts": f"{base}/v0/checks/<check>/artifacts",
        "check_log": f"{base}/v0/checks/<check>/logs/stdout?offset=0&limit=4096",
        "check_logs": f"{base}/v0/checks/<check>/logs/stdout",
        "conflicts": f"{base}/v0/conflicts?workspace=<workspace>&status=open",
        "conflict": f"{base}/v0/conflicts/<conflict>",
        "grant": f"{base}/v0/grants/<grant>",
        "artifacts": f"{base}/v0/artifacts?kind=stdout&limit=50",
        "artifact": f"{base}/v0/artifacts/<artifact>",
        "artifact_content": f"{base}/v0/artifacts/<artifact>/content",
        "events": f"{base}/v0/events?project=<project>&workspace=<workspace>&kind=<kind>&limit=50",
        "event": f"{base}/v0/events/<event>",
        "policy_decision": f"{base}/v0/policy/decisions/<decision>",
        "object": f"{base}/v0/<collection>/<id>",
    }


def api_server_data(host: str, port: int) -> dict[str, Any]:
    return {"host": host, "port": port, "endpoints": api_endpoint_catalog(host, port)}


class TriAPIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "tri-api/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        data = response_json(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise TriError("invalid_command", "Invalid Content-Length header.")
        if length > 10 * 1024 * 1024:
            raise TriError("invalid_command", "Request body too large.")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise TriError("invalid_command", f"Invalid JSON body: {exc.msg}")

    pinned_actor: str | None = None

    @property
    def server_paths_allowed(self) -> bool:
        """Server-local path arguments are only for an unauthenticated loopback
        server, whose callers already run on this machine."""
        host = str(self.server.server_address[0])
        return getattr(self.server, "authority", None) is None and host in LOOPBACK_HOSTS

    def authenticate(self, start: float) -> bool:
        """With a token file, bind the request to the token's actor or refuse it."""
        self.pinned_actor = None
        authority = getattr(self.server, "authority", None)
        if authority is None:
            return True
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else None
        actor = authority.actor_for(token)
        if actor is None:
            self.write_json(
                401,
                envelope(
                    "tri.error",
                    ["tri", "serve", "request"],
                    ok=False,
                    actor="actor_local",
                    start=start,
                    diagnostics=[TriError("unauthenticated", "A valid bearer token is required.").diagnostic],
                ),
            )
            return False
        claimed = self.headers.get("X-Tri-Actor")
        if claimed and claimed != actor:
            self.write_json(
                403,
                envelope(
                    "tri.error",
                    ["tri", "serve", "request"],
                    ok=False,
                    actor=actor,
                    start=start,
                    diagnostics=[TriError("actor_mismatch", "This client is authenticated as a different actor.", object=claimed, authenticated_actor=actor).diagnostic],
                ),
            )
            return False
        self.pinned_actor = actor
        return True

    def actor_from_request(self, payload: dict[str, Any] | None = None) -> str | None:
        claimed = payload["actor"] if payload and isinstance(payload.get("actor"), str) else None
        if self.pinned_actor is not None:
            if claimed and claimed != self.pinned_actor:
                raise_actor_mismatch(claimed, self.pinned_actor)
            return self.pinned_actor
        if claimed:
            return claimed
        header = self.headers.get("X-Tri-Actor")
        return header if header else None

    def run_get_command(self, start: float, argv: list[str]) -> bool:
        output, _ = execute_argv(argv, actor=self.actor_from_request(), start=start, pack_on_close=False, pinned_actor=self.pinned_actor, server_paths_allowed=self.server_paths_allowed, forward_remote=False)
        self.write_json(200, output)
        return True

    def diff_get_argv(self, object_id: str, parsed: Any) -> list[str]:
        query = parse_qs(parsed.query)
        argv = ["diff", object_id]
        mode = query.get("mode", [None])[-1]
        if mode:
            argv.extend(["--mode", mode])
        for path in query.get("path", []):
            argv.extend(["--path", path])
        return argv

    def query_flag(self, parsed: Any, *names: str) -> bool:
        query = parse_qs(parsed.query)
        for name in names:
            for value in query.get(name, []):
                if value.lower() in ("1", "true", "yes", "on"):
                    return True
        return False

    def query_value(self, parsed: Any, *names: str) -> str | None:
        query = parse_qs(parsed.query)
        for name in names:
            values = query.get(name)
            if values:
                return values[-1]
        return None

    def query_values(self, parsed: Any, *names: str) -> list[str]:
        query = parse_qs(parsed.query)
        values: list[str] = []
        for name in names:
            values.extend(query.get(name, []))
        return values

    def files_search_get_argv(self, object_id: str, parsed: Any) -> list[str]:
        argv = ["files", "search", object_id, self.query_value(parsed, "q", "query") or ""]
        glob = self.query_value(parsed, "glob")
        if glob:
            argv.extend(["--glob", glob])
        limit = self.query_value(parsed, "limit")
        if limit:
            argv.extend(["--limit", limit])
        if self.query_flag(parsed, "case-sensitive", "case_sensitive"):
            argv.append("--case-sensitive")
        return argv

    def project_search_get_argv(self, project: str, parsed: Any) -> list[str]:
        argv = ["search", project, self.query_value(parsed, "q", "query") or ""]
        for scope in self.query_values(parsed, "scope"):
            argv.extend(["--scope", scope])
        glob = self.query_value(parsed, "glob")
        if glob:
            argv.extend(["--glob", glob])
        limit = self.query_value(parsed, "limit")
        if limit:
            argv.extend(["--limit", limit])
        if self.query_flag(parsed, "case-sensitive", "case_sensitive"):
            argv.append("--case-sensitive")
        if self.query_flag(parsed, "include-history", "include_history"):
            argv.append("--include-history")
        return argv

    def events_list_get_argv(self, parsed: Any, *, project: str | None = None, workspace: str | None = None) -> list[str]:
        argv = ["events", "list"]
        project = project or self.query_value(parsed, "project")
        workspace = workspace or self.query_value(parsed, "workspace")
        if project:
            argv.extend(["--project", project])
        if workspace:
            argv.extend(["--workspace", workspace])
        kind = self.query_value(parsed, "kind")
        if kind:
            argv.extend(["--kind", kind])
        limit = self.query_value(parsed, "limit")
        if limit:
            argv.extend(["--limit", limit])
        return argv

    def artifacts_list_get_argv(self, parsed: Any) -> list[str]:
        argv = ["artifacts", "list"]
        kind = self.query_value(parsed, "kind")
        if kind:
            argv.extend(["--kind", kind])
        label = self.query_value(parsed, "label")
        if label:
            argv.extend(["--label", label])
        limit = self.query_value(parsed, "limit")
        if limit:
            argv.extend(["--limit", limit])
        return argv

    def export_list_get_argv(self, project: str, parsed: Any) -> list[str]:
        argv = ["export", "list", project]
        changeset = self.query_value(parsed, "changeset")
        if changeset:
            argv.extend(["--changeset", changeset])
        kind = self.query_value(parsed, "kind")
        if kind:
            argv.extend(["--kind", kind])
        limit = self.query_value(parsed, "limit")
        if limit:
            argv.extend(["--limit", limit])
        return argv

    def conflicts_list_get_argv(self, parsed: Any, *, workspace: str | None = None) -> list[str]:
        argv = ["conflicts", "list"]
        workspace = workspace or self.query_value(parsed, "workspace")
        if workspace:
            argv.extend(["--workspace", workspace])
        status = self.query_value(parsed, "status")
        if status:
            argv.extend(["--status", status])
        return argv

    def route_object_get(self, start: float, parsed: Any) -> bool:
        if not parsed.path.startswith("/v0/"):
            return False
        segments = [unquote(segment) for segment in parsed.path.removeprefix("/v0/").split("/") if segment]
        if len(segments) == 1:
            collection = segments[0]
            if collection == "projects":
                return self.run_get_command(start, ["projects", "list"])
            if collection == "artifacts":
                return self.run_get_command(start, self.artifacts_list_get_argv(parsed))
            if collection == "events":
                return self.run_get_command(start, self.events_list_get_argv(parsed))
            if collection == "conflicts":
                return self.run_get_command(start, self.conflicts_list_get_argv(parsed))
        if len(segments) == 2:
            collection, object_id = segments
            if collection == "objects":
                return self.run_get_command(start, ["objects", "inspect", object_id])
            if collection == "projects":
                return self.run_get_command(start, ["projects", "inspect", object_id])
            routes = {
                "actors": ["actors", "inspect", object_id],
                "packages": ["packages", "inspect", object_id],
                "tasks": ["task", "inspect", object_id],
                "task-runs": ["task", "run-inspect", object_id],
                "snapshots": ["snapshots", "inspect", object_id],
                "trees": ["trees", "inspect", object_id],
                "blobs": ["blobs", "inspect", object_id],
                "targets": ["targets", "inspect", object_id],
                "operations": ["operations", "inspect", object_id],
                "workspaces": ["workspace", "inspect", object_id],
                "leases": ["leases", "inspect", object_id],
                "changesets": ["changeset", "inspect", object_id],
                "reviews": ["review", "inspect", object_id],
                "review-actions": ["review", "action-inspect", object_id],
                "checks": ["checks", "inspect", object_id],
                "check-sets": ["checks", "set-inspect", object_id],
                "artifacts": ["artifacts", "inspect", object_id],
                "exports": ["export", "inspect", object_id],
                "events": ["events", "inspect", object_id],
                "releases": ["release", "inspect", object_id],
                "advisories": ["advisory", "inspect", object_id],
                "conflicts": ["conflicts", "inspect", object_id],
                "grants": ["policy", "grant-inspect", object_id],
                "secrets": ["secrets", "inspect", object_id],
            }
            argv = routes.get(collection)
            if argv:
                return self.run_get_command(start, argv)
        if len(segments) == 3:
            collection, object_id, subresource = segments
            if collection == "policy" and object_id == "decisions":
                return self.run_get_command(start, ["policy", "explain", subresource])
            if collection == "projects":
                routes = {
                    "targets": ["targets", "list", object_id],
                    "packages": ["packages", "list", object_id],
                    "snapshots": ["snapshots", "list", object_id],
                    "workspaces": ["workspace", "list", object_id],
                    "check-sets": ["checks", "set-list", object_id],
                    "advisories": ["advisory", "list", object_id],
                    "releases": ["release", "list", object_id],
                    "secrets": ["secrets", "list", object_id],
                }
                argv = routes.get(subresource)
                if argv:
                    return self.run_get_command(start, argv)
                if subresource == "search":
                    return self.run_get_command(start, self.project_search_get_argv(object_id, parsed))
                if subresource == "events":
                    return self.run_get_command(start, self.events_list_get_argv(parsed, project=object_id))
                if subresource == "exports":
                    return self.run_get_command(start, self.export_list_get_argv(object_id, parsed))
            if collection == "tasks" and subresource == "attempts":
                return self.run_get_command(start, ["task", "attempts", object_id])
            if collection in ("workspaces", "changesets") and subresource == "diff":
                return self.run_get_command(start, self.diff_get_argv(object_id, parsed))
            if collection in ("workspaces", "changesets") and subresource == "operations":
                return self.run_get_command(start, ["operations", "list", object_id])
            if collection in ("workspaces", "changesets") and subresource == "checks":
                return self.run_get_command(start, ["checks", "list", object_id])
            if collection == "snapshots" and subresource == "files":
                return self.run_get_command(start, ["files", "list", object_id])
            if collection == "workspaces" and subresource == "files":
                return self.run_get_command(start, ["files", "list", object_id])
            if collection == "workspaces" and subresource == "search":
                return self.run_get_command(start, self.files_search_get_argv(object_id, parsed))
            if collection == "workspaces" and subresource == "conflicts":
                return self.run_get_command(start, self.conflicts_list_get_argv(parsed, workspace=object_id))
            if collection == "workspaces" and subresource == "leases":
                return self.run_get_command(start, ["workspace", "leases", object_id])
            if collection == "workspaces" and subresource == "events":
                return self.run_get_command(start, self.events_list_get_argv(parsed, workspace=object_id))
            if collection == "checks" and subresource == "artifacts":
                return self.run_get_command(start, ["checks", "artifacts", object_id])
            if collection == "artifacts" and subresource == "content":
                return self.run_get_command(start, ["artifacts", "read", object_id])
        if len(segments) >= 4:
            collection, object_id, subresource = segments[:3]
            rest = segments[3:]
            if collection == "projects" and subresource == "targets" and len(rest) == 1:
                return self.run_get_command(start, ["targets", "inspect", object_id, rest[0]])
            if collection == "projects" and subresource == "packages" and len(rest) == 1:
                return self.run_get_command(start, ["packages", "inspect", object_id, rest[0]])
            if collection == "snapshots" and subresource == "files":
                return self.run_get_command(start, ["files", "read", object_id, "/".join(rest)])
            if collection == "workspaces" and subresource == "files":
                return self.run_get_command(start, ["files", "read", object_id, "/".join(rest)])
            if collection == "checks" and subresource == "logs" and len(rest) == 1:
                query = parse_qs(parsed.query)
                argv = ["checks", "stream", object_id, "--log", rest[0]]
                for name in ("offset", "limit"):
                    values = query.get(name)
                    if values:
                        argv.extend([f"--{name}", values[-1]])
                return self.run_get_command(start, argv)
        return False

    def do_GET(self) -> None:
        start = time.time()
        parsed = urlparse(self.path)
        if parsed.path not in ("/health", "/v0/health") and not self.authenticate(start):
            return
        if parsed.path in ("/v0", "/v0/"):
            host, port = self.server.server_address
            self.write_json(
                200,
                envelope(
                    "tri.api.server",
                    ["tri", "serve", "routes"],
                    actor=self.actor_from_request() or "actor_local",
                    start=start,
                    data=api_server_data(str(host), int(port)),
                ),
            )
            return
        if parsed.path in ("/health", "/v0/health"):
            store = Store(pack_on_close=False)
            try:
                data = {"status": "ok", "home": None if self.server.authority else str(store.home), "schema_version": SCHEMA_VERSION, "auth": "bearer" if self.server.authority else "none"}
            finally:
                store.close()
            self.write_json(200, envelope("tri.api.health", ["tri", "serve", "health"], actor=self.actor_from_request() or "actor_local", start=start, data=data))
            return
        if parsed.path == "/v0/commands":
            output, _ = execute_argv(["commands"], actor=self.actor_from_request(), start=start, pack_on_close=False, pinned_actor=self.pinned_actor, server_paths_allowed=self.server_paths_allowed, forward_remote=False)
            self.write_json(200, output)
            return
        if parsed.path == "/v0/schema":
            output, _ = execute_argv(["schema"], actor=self.actor_from_request(), start=start, pack_on_close=False, pinned_actor=self.pinned_actor, server_paths_allowed=self.server_paths_allowed, forward_remote=False)
            self.write_json(200, output)
            return
        if parsed.path.startswith("/v0/schema/"):
            name = unquote(parsed.path.removeprefix("/v0/schema/"))
            output, _ = execute_argv(["schema", name], actor=self.actor_from_request(), start=start, pack_on_close=False, pinned_actor=self.pinned_actor, server_paths_allowed=self.server_paths_allowed, forward_remote=False)
            self.write_json(200, output)
            return
        if self.route_object_get(start, parsed):
            return
        self.write_json(
            404,
            envelope(
                "tri.error",
                ["tri", "serve", "request"],
                ok=False,
                actor=self.actor_from_request() or "actor_local",
                start=start,
                diagnostics=[TriError("path_not_found", "API endpoint not found.", object=parsed.path).diagnostic],
            ),
        )

    def do_POST(self) -> None:
        start = time.time()
        parsed = urlparse(self.path)
        if not self.authenticate(start):
            return
        if parsed.path not in ("/v0/command", "/v0/batch"):
            self.write_json(
                404,
                envelope(
                    "tri.error",
                    ["tri", "serve", "request"],
                    ok=False,
                    actor=self.actor_from_request() or "actor_local",
                    start=start,
                    diagnostics=[TriError("path_not_found", "API endpoint not found.", object=parsed.path).diagnostic],
                ),
            )
            return
        try:
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise TriError("invalid_command", "Command request must be a JSON object.")
            if parsed.path == "/v0/batch":
                store = Store(pack_on_close=False)
                try:
                    actor = self.actor_from_request(payload) or "actor_local"
                    data, ok = run_batch_payload(store, payload, default_actor=actor, start=start, pinned_actor=self.pinned_actor, server_paths_allowed=self.server_paths_allowed)
                finally:
                    store.close()
                self.write_json(
                    200,
                    envelope(
                        "tri.api.batch",
                        ["tri", "serve", "batch"],
                        ok=ok,
                        actor=self.actor_from_request(payload) or "actor_local",
                        start=start,
                        data=data,
                    ),
                )
                return
            argv = payload.get("argv")
            if argv is None:
                argv = payload.get("command")
            if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                raise TriError("invalid_command", "Command request requires argv as a list of strings.")
            if argv and argv[0] == "tri":
                argv = argv[1:]
            if argv and argv[0] in API_BLOCKED_COMMANDS:
                raise TriError("invalid_command", "This command is not available through the API.", object=argv[0])
            output, _ = execute_argv(argv, actor=self.actor_from_request(payload), start=start, pack_on_close=False, pinned_actor=self.pinned_actor, server_paths_allowed=self.server_paths_allowed, forward_remote=False)
            self.write_json(200, output)
        except TriError as exc:
            self.write_json(
                200,
                envelope(
                    "tri.error",
                    ["tri", "serve", "request"],
                    ok=False,
                    actor=self.actor_from_request() or "actor_local",
                    start=start,
                    diagnostics=[exc.diagnostic],
                ),
            )


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def cmd_mcp(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    from tri.mcp import SUPPORTED_PROTOCOL_VERSIONS, TriMCPServer, serve_stdio, tool_definitions

    actor = args.actor
    require_known_actor(actor)
    if args.run_sandbox:
        os.environ[sandbox.MODE_ENV] = args.run_sandbox
        if args.run_sandbox == "required" and not sandbox.probe()["isolated"]:
            raise TriError("sandbox_unavailable", "Sandboxed runs were required, but this host cannot isolate commands.", reason=sandbox.probe().get("reason"))

    def execute(argv: list[str]) -> dict[str, Any]:
        if argv and argv[0] in API_BLOCKED_COMMANDS:
            return envelope("tri.error", ["tri", *argv], ok=False, actor=actor, start=time.time(), diagnostics=[TriError("invalid_command", "This command is not available through MCP.", object=argv[0]).diagnostic])
        output, _ = execute_argv(argv, start=time.time(), pinned_actor=actor)
        return output

    def batch(payload: dict[str, Any]) -> dict[str, Any]:
        batch_start = time.time()
        batch_store = Store()
        try:
            data, ok = run_batch_payload(batch_store, payload, default_actor=actor, start=batch_start, pinned_actor=actor)
            return envelope("tri.batch", ["tri", "batch"], actor=actor, start=batch_start, ok=ok, data=data)
        except TriError as exc:
            return envelope("tri.error", ["tri", "batch"], ok=False, actor=actor, start=batch_start, diagnostics=[exc.diagnostic])
        finally:
            batch_store.close()

    serve_stdio(TriMCPServer(actor=actor, execute=execute, batch=batch))
    return envelope(
        "tri.mcp.server",
        command,
        actor=actor,
        start=start,
        data={"protocol_version": SUPPORTED_PROTOCOL_VERSIONS[0], "tools": [tool["name"] for tool in tool_definitions(actor)], "actor": actor},
    )


def cmd_auth_issue(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_maintainer(args.actor, action="auth.issue", object_id=args.identity, message="Only maintainers can issue API tokens.")
    require_known_actor(args.identity)
    token, record = issue_token(Path(args.file).resolve(), args.identity, label=args.label)
    return envelope("tri.auth.token", command, actor=args.actor, start=start, data={**public_record(record), "token": token, "file": str(Path(args.file).resolve()), "shown_once": True})


def cmd_auth_list(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_maintainer(args.actor, action="auth.list", object_id=args.file, message="Only maintainers can list API tokens.")
    tokens = [public_record(item) for item in load_token_file(Path(args.file).resolve())]
    return envelope("tri.auth.list", command, actor=args.actor, start=start, data={"file": str(Path(args.file).resolve()), "tokens": tokens})


def cmd_auth_revoke(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    require_maintainer(args.actor, action="auth.revoke", object_id=args.selector, message="Only maintainers can revoke API tokens.")
    removed = revoke_tokens(Path(args.file).resolve(), args.selector)
    if not removed:
        raise TriError("path_not_found", "No token matches that id or actor.", object=args.selector)
    return envelope("tri.auth.list", command, actor=args.actor, start=start, data={"file": str(Path(args.file).resolve()), "tokens": [public_record(item) for item in removed]})


def cmd_serve(store: Store, args: argparse.Namespace, command: list[str], start: float) -> dict[str, Any]:
    auth_file = args.auth_file or os.environ.get("TRI_SERVE_AUTH_FILE")
    if not auth_file and args.host not in LOOPBACK_HOSTS and not args.allow_unauthenticated:
        raise TriError(
            "unauthenticated",
            "Refusing to serve beyond loopback without --auth-file: any client could act as any actor.",
            object=args.host,
            required_flag="--auth-file",
        )
    run_sandbox = args.run_sandbox or os.environ.get(sandbox.MODE_ENV) or ("off" if args.host in LOOPBACK_HOSTS else "required")
    if run_sandbox == "required" and not sandbox.probe()["isolated"] and not args.allow_unsandboxed_runs:
        raise TriError(
            "sandbox_unavailable",
            "Refusing to serve beyond loopback without sandboxed runs: any client could execute code as the server user.",
            reason=sandbox.probe().get("reason"),
            hint="Install bubblewrap, or pass --allow-unsandboxed-runs if every token holder is trusted.",
        )
    os.environ[sandbox.MODE_ENV] = "auto" if run_sandbox == "required" and args.allow_unsandboxed_runs else run_sandbox
    server = TriHTTPServer((args.host, args.port), TriAPIHandler)
    server.authority = TokenAuthority(Path(auth_file).resolve()) if auth_file else None
    host, port = server.server_address
    data = {**api_server_data(str(host), int(port)), "auth": "bearer" if auth_file else "none", "run_sandbox": os.environ[sandbox.MODE_ENV]}
    if not args.quiet:
        print_json(envelope("tri.api.server", command, actor=args.actor, start=start, data=data))
        sys.stdout.flush()
    reaper_stop = start_reaper_thread() if os.environ.get("TRI_AUTO_REAP", "1") != "0" else None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if reaper_stop is not None:
            reaper_stop.set()
        server.server_close()
    return envelope("tri.api.server", command, actor=args.actor, start=start, data=data)


def build_parser() -> argparse.ArgumentParser:
    parser = TriArgumentParser(prog="tri")
    parser.add_argument("--actor", default=os.environ.get("TRI_ACTOR", "actor_local"))
    parser.add_argument("--response", choices=["full", "summary"], default=os.environ.get("TRI_RESPONSE", "full"))
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=TriArgumentParser)

    p = sub.add_parser("commands")
    p.set_defaults(func=cmd_commands)

    p = sub.add_parser("schema")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("batch")
    p.add_argument("plan")
    p.add_argument("--continue-on-error", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gc")
    p.add_argument("--reap", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--retention-days", type=float)
    p.add_argument("--grace-seconds", type=float)
    p.add_argument("--unsafe-no-grace", action="store_true")
    p.add_argument("--clear-caches", action="store_true")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("env")
    env_sub = p.add_subparsers(dest="env_cmd", required=True)
    p_env = env_sub.add_parser("define")
    p_env.add_argument("project")
    p_env.add_argument("name")
    p_env.add_argument("--key-file", action="append")
    p_env.add_argument("--key-command", action="append")
    p_env.add_argument("--install", required=True)
    p_env.add_argument("--output", action="append")
    p_env.add_argument("--mode", default="auto")
    p_env.set_defaults(func=cmd_env_define)
    p_env = env_sub.add_parser("list")
    p_env.add_argument("project")
    p_env.set_defaults(func=cmd_env_list)
    p_env = env_sub.add_parser("inspect")
    p_env.add_argument("project")
    p_env.add_argument("name")
    p_env.set_defaults(func=cmd_env_inspect)
    p_env = env_sub.add_parser("remove")
    p_env.add_argument("project")
    p_env.add_argument("name")
    p_env.set_defaults(func=cmd_env_remove)

    p = sub.add_parser("ignore")
    ignore_sub = p.add_subparsers(dest="ignore_cmd", required=True)
    p_ignore = ignore_sub.add_parser("add")
    p_ignore.add_argument("project")
    p_ignore.add_argument("patterns", nargs="+")
    p_ignore.set_defaults(func=cmd_ignore_add)
    p_ignore = ignore_sub.add_parser("remove")
    p_ignore.add_argument("project")
    p_ignore.add_argument("patterns", nargs="+")
    p_ignore.set_defaults(func=cmd_ignore_remove)
    p_ignore = ignore_sub.add_parser("list")
    p_ignore.add_argument("project")
    p_ignore.set_defaults(func=cmd_ignore_list)

    p = sub.add_parser("objects")
    objects_sub = p.add_subparsers(dest="objects_cmd", required=True)
    p_inspect = objects_sub.add_parser("inspect")
    p_inspect.add_argument("object")
    p_inspect.set_defaults(func=cmd_objects_inspect)

    p = sub.add_parser("actors")
    actors_sub = p.add_subparsers(dest="actors_cmd", required=True)
    p_inspect = actors_sub.add_parser("inspect")
    p_inspect.add_argument("identity")
    p_inspect.set_defaults(func=cmd_actors_inspect)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--auth-file")
    p.add_argument("--allow-unauthenticated", action="store_true")
    p.add_argument("--run-sandbox", choices=list(sandbox.MODES))
    p.add_argument("--allow-unsandboxed-runs", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp")
    p.add_argument("--run-sandbox", choices=list(sandbox.MODES))
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("auth")
    auth_sub = p.add_subparsers(dest="auth_cmd", required=True)
    p_auth = auth_sub.add_parser("issue")
    p_auth.add_argument("identity")
    p_auth.add_argument("--file", required=True)
    p_auth.add_argument("--label")
    p_auth.set_defaults(func=cmd_auth_issue)
    p_auth = auth_sub.add_parser("list")
    p_auth.add_argument("--file", required=True)
    p_auth.set_defaults(func=cmd_auth_list)
    p_auth = auth_sub.add_parser("revoke")
    p_auth.add_argument("selector")
    p_auth.add_argument("--file", required=True)
    p_auth.set_defaults(func=cmd_auth_revoke)

    p = sub.add_parser("import")
    p.add_argument("repo")
    p.add_argument("--name", required=True)
    p.add_argument("--ref")
    p.add_argument("--target")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("projects")
    projects_sub = p.add_subparsers(dest="projects_cmd", required=True)
    p_list = projects_sub.add_parser("list")
    p_list.set_defaults(func=cmd_projects_list)
    p_inspect = projects_sub.add_parser("inspect")
    p_inspect.add_argument("project")
    p_inspect.set_defaults(func=cmd_projects_inspect)

    p = sub.add_parser("packages")
    packages_sub = p.add_subparsers(dest="packages_cmd", required=True)
    p_declare = packages_sub.add_parser("declare")
    p_declare.add_argument("project")
    p_declare.add_argument("name")
    p_declare.add_argument("--path", action="append")
    p_declare.add_argument("--visibility", action="append")
    p_declare.add_argument("--owner", action="append")
    p_declare.set_defaults(func=cmd_packages_declare)
    p_list = packages_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.set_defaults(func=cmd_packages_list)
    p_inspect = packages_sub.add_parser("inspect")
    p_inspect.add_argument("project")
    p_inspect.add_argument("name", nargs="?")
    p_inspect.set_defaults(func=cmd_packages_inspect)

    p = sub.add_parser("targets")
    target_sub = p.add_subparsers(dest="target_cmd", required=True)
    p_list = target_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.set_defaults(func=cmd_targets_list)
    p_inspect = target_sub.add_parser("inspect")
    p_inspect.add_argument("project")
    p_inspect.add_argument("target", nargs="?")
    p_inspect.set_defaults(func=cmd_targets_inspect)
    p_refresh = target_sub.add_parser("refresh")
    p_refresh.add_argument("project")
    p_refresh.add_argument("target")
    p_refresh.add_argument("--ref")
    p_refresh.add_argument("--expect-snapshot")
    p_refresh.set_defaults(func=cmd_targets_refresh)

    p = sub.add_parser("snapshots")
    snapshot_sub = p.add_subparsers(dest="snapshots_cmd", required=True)
    p_list = snapshot_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.set_defaults(func=cmd_snapshots_list)
    p_inspect = snapshot_sub.add_parser("inspect")
    p_inspect.add_argument("snapshot")
    p_inspect.set_defaults(func=cmd_snapshots_inspect)
    p_base = snapshot_sub.add_parser("merge-base")
    p_base.add_argument("left")
    p_base.add_argument("right")
    p_base.set_defaults(func=cmd_snapshots_merge_base)

    p = sub.add_parser("trees")
    trees_sub = p.add_subparsers(dest="trees_cmd", required=True)
    p_inspect = trees_sub.add_parser("inspect")
    p_inspect.add_argument("tree")
    p_inspect.set_defaults(func=cmd_trees_inspect)

    p = sub.add_parser("blobs")
    blobs_sub = p.add_subparsers(dest="blobs_cmd", required=True)
    p_inspect = blobs_sub.add_parser("inspect")
    p_inspect.add_argument("blob")
    p_inspect.set_defaults(func=cmd_blobs_inspect)

    p = sub.add_parser("task")
    task_sub = p.add_subparsers(dest="task_cmd", required=True)
    p_create = task_sub.add_parser("create")
    p_create.add_argument("project")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body")
    p_create.add_argument("--body-file")
    p_create.add_argument("--acceptance", action="append")
    p_create.add_argument("--required-check", action="append")
    p_create.add_argument("--check-set", action="append")
    p_create.set_defaults(func=cmd_task_create)
    p_inspect = task_sub.add_parser("inspect")
    p_inspect.add_argument("task")
    p_inspect.set_defaults(func=cmd_task_inspect)
    p_run_inspect = task_sub.add_parser("run-inspect")
    p_run_inspect.add_argument("run")
    p_run_inspect.set_defaults(func=cmd_task_run_inspect)
    p_complete = task_sub.add_parser("complete-run")
    p_complete.add_argument("run")
    p_complete.add_argument("--recommendation", required=True)
    p_complete.add_argument("--status", choices=["completed", "failed", "cancelled"], default="completed")
    p_complete.set_defaults(func=cmd_task_complete_run)
    p_block = task_sub.add_parser("block")
    p_block.add_argument("task")
    p_block.add_argument("--reason", required=True)
    p_block.set_defaults(func=cmd_task_block)
    p_resume = task_sub.add_parser("resume")
    p_resume.add_argument("task")
    p_resume.add_argument("--reason")
    p_resume.set_defaults(func=cmd_task_resume)
    p_accept = task_sub.add_parser("accept")
    p_accept.add_argument("task")
    p_accept.add_argument("--changeset", action="append")
    p_accept.add_argument("--no-change")
    p_accept.set_defaults(func=cmd_task_accept)
    p_reject = task_sub.add_parser("reject")
    p_reject.add_argument("task")
    p_reject.add_argument("--reason", required=True)
    p_reject.set_defaults(func=cmd_task_reject)
    p_close = task_sub.add_parser("close")
    p_close.add_argument("task")
    p_close.add_argument("--reason")
    p_close.set_defaults(func=cmd_task_close)
    p_attempts = task_sub.add_parser("attempts")
    p_attempts.add_argument("task")
    p_attempts.set_defaults(func=cmd_task_attempts)

    p = sub.add_parser("workspace")
    workspace_sub = p.add_subparsers(dest="workspace_cmd", required=True)
    p_list = workspace_sub.add_parser("list")
    p_list.add_argument("project", nargs="?")
    p_list.set_defaults(func=cmd_workspace_list)
    p_create = workspace_sub.add_parser("create")
    p_create.add_argument("project")
    p_create.add_argument("--from", dest="from_ref", required=True)
    p_create.add_argument("--task")
    p_create.add_argument("--visibility", action="append")
    p_create.add_argument("--hide-path", action="append")
    p_create.add_argument("--hide-package", action="append")
    p_create.add_argument("--advisory")
    p_create.set_defaults(func=cmd_workspace_create)
    p_inspect = workspace_sub.add_parser("inspect")
    p_inspect.add_argument("workspace")
    p_inspect.set_defaults(func=cmd_workspace_inspect)
    p_update = workspace_sub.add_parser("update")
    p_update.add_argument("workspace")
    p_update.add_argument("--from", dest="from_ref", required=True)
    p_update.add_argument("--expect-head")
    p_update.set_defaults(func=cmd_workspace_update)
    p_snapshot = workspace_sub.add_parser("snapshot")
    p_snapshot.add_argument("workspace")
    p_snapshot.add_argument("--expect-head")
    p_snapshot.set_defaults(func=cmd_workspace_snapshot)
    p_mount = workspace_sub.add_parser("mount")
    p_mount.add_argument("workspace")
    p_mount.add_argument("path")
    p_mount.add_argument("--ttl-seconds", type=int)
    p_mount.add_argument("--no-ttl", action="store_true")
    p_mount.add_argument("--no-env", action="store_true")
    p_mount.add_argument("--env-link", action="store_true")
    p_mount.add_argument("--ignore-path", action="append")
    p_mount.add_argument("--paths", action="append")
    p_mount.set_defaults(func=cmd_workspace_mount)
    p_sync = workspace_sub.add_parser("sync")
    p_sync.add_argument("workspace")
    p_sync.add_argument("--lease")
    p_sync.set_defaults(func=cmd_workspace_sync)
    p_leases = workspace_sub.add_parser("leases")
    p_leases.add_argument("workspace")
    p_leases.set_defaults(func=cmd_workspace_leases)
    p_lease_inspect = workspace_sub.add_parser("lease-inspect")
    p_lease_inspect.add_argument("lease")
    p_lease_inspect.set_defaults(func=cmd_workspace_lease_inspect)
    p_lease_revoke = workspace_sub.add_parser("lease-revoke")
    p_lease_revoke.add_argument("lease")
    p_lease_revoke.set_defaults(func=cmd_workspace_lease_revoke)

    p = sub.add_parser("leases")
    leases_sub = p.add_subparsers(dest="leases_cmd", required=True)
    p_inspect = leases_sub.add_parser("inspect")
    p_inspect.add_argument("lease")
    p_inspect.set_defaults(func=cmd_leases_inspect)
    p_lease_list = leases_sub.add_parser("list")
    p_lease_list.add_argument("--workspace")
    p_lease_list.add_argument("--status")
    p_lease_list.set_defaults(func=cmd_leases_list)
    p_lease_renew = leases_sub.add_parser("renew")
    p_lease_renew.add_argument("lease")
    p_lease_renew.add_argument("--ttl-seconds", type=int)
    p_lease_renew.set_defaults(func=cmd_leases_renew)
    p_lease_reap = leases_sub.add_parser("reap")
    p_lease_reap.add_argument("lease", nargs="?")
    p_lease_reap.add_argument("--force", action="store_true")
    p_lease_reap.set_defaults(func=cmd_leases_reap)

    p = sub.add_parser("salvage")
    salvage_sub = p.add_subparsers(dest="salvage_cmd", required=True)
    p_salvage = salvage_sub.add_parser("list")
    p_salvage.add_argument("workspace")
    p_salvage.set_defaults(func=cmd_salvage_list)
    p_salvage = salvage_sub.add_parser("adopt")
    p_salvage.add_argument("salvage")
    p_salvage.add_argument("--expect-head")
    p_salvage.set_defaults(func=cmd_salvage_adopt)

    p_pause = workspace_sub.add_parser("pause")
    p_pause.add_argument("workspace")
    p_pause.set_defaults(func=cmd_workspace_pause)
    p_resume = workspace_sub.add_parser("resume")
    p_resume.add_argument("workspace")
    p_resume.set_defaults(func=cmd_workspace_resume)
    p_discard = workspace_sub.add_parser("discard")
    p_discard.add_argument("workspace")
    p_discard.set_defaults(func=cmd_workspace_discard)
    p_archive = workspace_sub.add_parser("archive")
    p_archive.add_argument("workspace")
    p_archive.set_defaults(func=cmd_workspace_archive)

    p = sub.add_parser("conflicts")
    conflicts_sub = p.add_subparsers(dest="conflicts_cmd", required=True)
    p_list = conflicts_sub.add_parser("list")
    p_list.add_argument("--workspace")
    p_list.add_argument("--status")
    p_list.set_defaults(func=cmd_conflicts_list)
    p_inspect = conflicts_sub.add_parser("inspect")
    p_inspect.add_argument("conflict")
    p_inspect.set_defaults(func=cmd_conflicts_inspect)
    p_resolve = conflicts_sub.add_parser("resolve")
    p_resolve.add_argument("conflict")
    p_resolve.add_argument("--strategy", choices=["ours", "theirs", "base"], required=True)
    p_resolve.set_defaults(func=cmd_conflicts_resolve)

    p = sub.add_parser("files")
    files_sub = p.add_subparsers(dest="files_cmd", required=True)
    p_list = files_sub.add_parser("list")
    p_list.add_argument("ref")
    p_list.set_defaults(func=cmd_files_list)
    p_stat = files_sub.add_parser("stat")
    p_stat.add_argument("ref")
    p_stat.add_argument("path")
    p_stat.set_defaults(func=cmd_files_stat)
    p_read = files_sub.add_parser("read")
    p_read.add_argument("ref")
    p_read.add_argument("path")
    p_read.add_argument("--offset", type=int, default=0)
    p_read.add_argument("--limit", type=int)
    p_read.set_defaults(func=cmd_files_read)
    p_fetch = files_sub.add_parser("fetch")
    p_fetch.add_argument("ref")
    p_fetch.add_argument("--path", action="append")
    p_fetch.add_argument("--max-bytes", type=int)
    p_fetch.set_defaults(func=cmd_files_fetch)
    p_write = files_sub.add_parser("write")
    p_write.add_argument("workspace")
    p_write.add_argument("path")
    p_write.add_argument("--body")
    p_write.add_argument("--text")
    p_write.add_argument("--base64")
    p_write.add_argument("--expect-head")
    p_write.set_defaults(func=cmd_files_write)
    p_delete = files_sub.add_parser("delete")
    p_delete.add_argument("workspace")
    p_delete.add_argument("path")
    p_delete.add_argument("--expect-head")
    p_delete.set_defaults(func=cmd_files_delete)
    p_move = files_sub.add_parser("move")
    p_move.add_argument("workspace")
    p_move.add_argument("src")
    p_move.add_argument("dest")
    p_move.add_argument("--expect-head")
    p_move.set_defaults(func=cmd_files_move)
    p_log = files_sub.add_parser("log")
    p_log.add_argument("ref")
    p_log.add_argument("path")
    p_log.add_argument("--limit", type=int, default=50)
    p_log.set_defaults(func=cmd_files_log)
    p_blame = files_sub.add_parser("blame")
    p_blame.add_argument("ref")
    p_blame.add_argument("path")
    p_blame.set_defaults(func=cmd_files_blame)
    p_search = files_sub.add_parser("search")
    p_search.add_argument("ref")
    p_search.add_argument("query")
    p_search.add_argument("--glob")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--case-sensitive", action="store_true")
    p_search.set_defaults(func=cmd_files_search)

    p = sub.add_parser("operations")
    operations_sub = p.add_subparsers(dest="operations_cmd", required=True)
    p_list = operations_sub.add_parser("list")
    p_list.add_argument("ref")
    p_list.set_defaults(func=cmd_operations_list)
    p_inspect = operations_sub.add_parser("inspect")
    p_inspect.add_argument("operation")
    p_inspect.set_defaults(func=cmd_operations_inspect)

    p = sub.add_parser("search")
    p.add_argument("project")
    p.add_argument("query")
    p.add_argument("--scope", action="append", choices=["all", *SEARCH_SCOPES])
    p.add_argument("--glob")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--include-history", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("diff")
    p.add_argument("ref")
    p.add_argument("head_ref", nargs="?")
    p.add_argument("--mode", choices=["summary", "files", "patch", "machine"], default="machine")
    p.add_argument("--path", action="append")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("run")
    p.add_argument("workspace")
    p.add_argument("--with-lease", action="append")
    p.add_argument("--assumption", action="append")
    p.add_argument("--no-import-changes", action="store_true")
    p.add_argument("--no-env", action="store_true")
    p.add_argument("--paths", action="append")
    p.add_argument("--sandbox", action="store_true")
    p.add_argument("--timeout", type=float)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("merge-rules")
    merge_rules_sub = p.add_subparsers(dest="merge_rules_cmd", required=True)
    p_rule = merge_rules_sub.add_parser("add")
    p_rule.add_argument("project")
    p_rule.add_argument("name")
    p_rule.add_argument("--path", action="append")
    p_rule.add_argument("--exclude", action="append")
    p_rule.add_argument("--allow", action="append")
    p_rule.add_argument("--max-files", type=int)
    p_rule.add_argument("--verify-after")
    p_rule.set_defaults(func=cmd_merge_rules_add)
    p_rule = merge_rules_sub.add_parser("list")
    p_rule.add_argument("project")
    p_rule.add_argument("--all", action="store_true")
    p_rule.set_defaults(func=cmd_merge_rules_list)
    p_rule = merge_rules_sub.add_parser("inspect")
    p_rule.add_argument("project")
    p_rule.add_argument("name")
    p_rule.set_defaults(func=cmd_merge_rules_inspect)
    p_rule = merge_rules_sub.add_parser("remove")
    p_rule.add_argument("project")
    p_rule.add_argument("name")
    p_rule.set_defaults(func=cmd_merge_rules_remove)

    p = sub.add_parser("verify")
    verify_sub = p.add_subparsers(dest="verify_cmd", required=True)
    p_verify = verify_sub.add_parser("run")
    p_verify.add_argument("project")
    p_verify.add_argument("--limit", type=int, default=50)
    p_verify.add_argument("--watch", action="store_true")
    p_verify.add_argument("--interval", type=float, default=10.0)
    p_verify.add_argument("--max-cycles", type=int)
    p_verify.set_defaults(func=cmd_verify_run)
    p_verify = verify_sub.add_parser("list")
    p_verify.add_argument("project")
    p_verify.add_argument("--status")
    p_verify.set_defaults(func=cmd_verify_list)
    p_verify = verify_sub.add_parser("inspect")
    p_verify.add_argument("verification")
    p_verify.set_defaults(func=cmd_verify_inspect)

    p = sub.add_parser("mirror")
    mirror_sub = p.add_subparsers(dest="mirror_cmd", required=True)
    p_mirror = mirror_sub.add_parser("add")
    p_mirror.add_argument("project")
    p_mirror.add_argument("--remote", required=True)
    p_mirror.add_argument("--target", default="main")
    p_mirror.add_argument("--branch")
    p_mirror.add_argument("--audience")
    p_mirror.add_argument("--prefer", choices=["git", "tri"])
    p_mirror.add_argument("--retry", type=int, default=3)
    p_mirror.add_argument("--no-sync", action="store_true")
    p_mirror.set_defaults(func=cmd_mirror_add)
    p_mirror = mirror_sub.add_parser("list")
    p_mirror.add_argument("project", nargs="?")
    p_mirror.add_argument("--all", action="store_true")
    p_mirror.set_defaults(func=cmd_mirror_list)
    p_mirror = mirror_sub.add_parser("inspect")
    p_mirror.add_argument("mirror")
    p_mirror.set_defaults(func=cmd_mirror_inspect)
    p_mirror = mirror_sub.add_parser("sync")
    p_mirror.add_argument("mirror")
    p_mirror.add_argument("--retry", type=int, default=5)
    p_mirror.add_argument("--watch", action="store_true")
    p_mirror.add_argument("--interval", type=float, default=30.0)
    p_mirror.add_argument("--max-cycles", type=int)
    p_mirror.set_defaults(func=cmd_mirror_sync)
    p_mirror = mirror_sub.add_parser("remove")
    p_mirror.add_argument("mirror")
    p_mirror.set_defaults(func=cmd_mirror_remove)

    p = sub.add_parser("checks")
    checks_sub = p.add_subparsers(dest="checks_cmd", required=True)
    p_list = checks_sub.add_parser("list")
    p_list.add_argument("ref")
    p_list.set_defaults(func=cmd_checks_list)
    p_inspect = checks_sub.add_parser("inspect")
    p_inspect.add_argument("check")
    p_inspect.set_defaults(func=cmd_checks_inspect)
    p_artifacts = checks_sub.add_parser("artifacts")
    p_artifacts.add_argument("check")
    p_artifacts.set_defaults(func=cmd_checks_artifacts)
    p_stream = checks_sub.add_parser("stream")
    p_stream.add_argument("check")
    p_stream.add_argument("--log", choices=["stdout", "stderr"], default="stdout")
    p_stream.add_argument("--offset", type=int, default=0)
    p_stream.add_argument("--limit", type=int, default=4096)
    p_stream.set_defaults(func=cmd_checks_stream)
    p_rerun = checks_sub.add_parser("rerun")
    p_rerun.add_argument("check")
    p_rerun.set_defaults(func=cmd_checks_rerun)
    p_compare = checks_sub.add_parser("compare")
    p_compare.add_argument("checks", nargs="+")
    p_compare.set_defaults(func=cmd_checks_compare)
    p_set_create = checks_sub.add_parser("set-create")
    p_set_create.add_argument("project")
    p_set_create.add_argument("name")
    p_set_create.add_argument("--required-check", action="append")
    p_set_create.set_defaults(func=cmd_check_set_create)
    p_set_list = checks_sub.add_parser("set-list")
    p_set_list.add_argument("project")
    p_set_list.set_defaults(func=cmd_check_set_list)
    p_set_inspect = checks_sub.add_parser("set-inspect")
    p_set_inspect.add_argument("project")
    p_set_inspect.add_argument("name", nargs="?")
    p_set_inspect.set_defaults(func=cmd_check_set_inspect)

    p = sub.add_parser("changeset")
    cs_sub = p.add_subparsers(dest="changeset_cmd", required=True)
    p_create = cs_sub.add_parser("create")
    p_create.add_argument("workspace")
    p_create.add_argument("--title")
    p_create.add_argument("--hide-path", action="append")
    p_create.add_argument("--hide-package", action="append")
    p_create.add_argument("--visibility", action="append")
    p_create.set_defaults(func=cmd_changeset_create)
    p_inspect = cs_sub.add_parser("inspect")
    p_inspect.add_argument("changeset")
    p_inspect.set_defaults(func=cmd_changeset_inspect)
    p_split = cs_sub.add_parser("split")
    p_split.add_argument("changeset")
    split_selector = p_split.add_mutually_exclusive_group(required=True)
    split_selector.add_argument("--paths")
    split_selector.add_argument("--operations")
    p_split.add_argument("--apply", action="store_true")
    p_split.add_argument("--title")
    p_split.set_defaults(func=cmd_changeset_split)
    p_stack = cs_sub.add_parser("stack")
    p_stack.add_argument("changeset")
    p_stack.add_argument("--after", required=True)
    p_stack.set_defaults(func=cmd_changeset_stack)
    p_revalidate = cs_sub.add_parser("revalidate")
    p_revalidate.add_argument("changeset")
    p_revalidate.add_argument("--check", action="append")
    p_revalidate.add_argument("--required", action="store_true")
    p_revalidate.set_defaults(func=cmd_changeset_revalidate)
    p_ready = cs_sub.add_parser("ready")
    p_ready.add_argument("changeset")
    p_ready.add_argument("--reason")
    p_ready.set_defaults(func=cmd_changeset_ready)
    p_discard = cs_sub.add_parser("discard")
    p_discard.add_argument("changeset")
    p_discard.add_argument("--reason")
    p_discard.set_defaults(func=cmd_changeset_discard)
    p_supersede = cs_sub.add_parser("supersede")
    p_supersede.add_argument("changeset")
    p_supersede.add_argument("--by", dest="replacement", required=True)
    p_supersede.add_argument("--reason")
    p_supersede.set_defaults(func=cmd_changeset_supersede)
    p_merge = cs_sub.add_parser("merge")
    p_merge.add_argument("changeset")
    p_merge.add_argument("--target", required=True)
    p_merge.add_argument("--retry", type=int, default=5)
    p_merge.set_defaults(func=cmd_changeset_merge)
    p_compare = cs_sub.add_parser("compare")
    p_compare.add_argument("changesets", nargs="+")
    p_compare.set_defaults(func=cmd_changeset_compare)

    p = sub.add_parser("review")
    review_sub = p.add_subparsers(dest="review_cmd", required=True)
    p_create = review_sub.add_parser("create")
    p_create.add_argument("changeset")
    p_create.add_argument("--reviewers", action="append")
    p_create.set_defaults(func=cmd_review_create)
    p_inspect = review_sub.add_parser("inspect")
    p_inspect.add_argument("review")
    p_inspect.set_defaults(func=cmd_review_inspect)
    p_action_inspect = review_sub.add_parser("action-inspect")
    p_action_inspect.add_argument("action")
    p_action_inspect.set_defaults(func=cmd_review_action_inspect)
    p_comment = review_sub.add_parser("comment")
    p_comment.add_argument("review")
    p_comment.add_argument("--body", required=True)
    p_comment.add_argument("--path")
    p_comment.add_argument("--line", type=int)
    p_comment.add_argument("--reply-to")
    p_comment.set_defaults(func=cmd_review_comment)
    p_resolve_thread = review_sub.add_parser("resolve-thread")
    p_resolve_thread.add_argument("review")
    p_resolve_thread.add_argument("--thread", required=True)
    p_resolve_thread.add_argument("--body")
    p_resolve_thread.set_defaults(func=cmd_review_resolve_thread)
    p_request_changes = review_sub.add_parser("request-changes")
    p_request_changes.add_argument("review")
    p_request_changes.add_argument("--body", required=True)
    p_request_changes.set_defaults(func=cmd_review_request_changes)
    p_approve = review_sub.add_parser("approve")
    p_approve.add_argument("review")
    p_approve.add_argument("--body")
    p_approve.set_defaults(func=cmd_review_approve)
    p_risk = review_sub.add_parser("mark-risk")
    p_risk.add_argument("review")
    p_risk.add_argument("--risk", required=True)
    p_risk.add_argument("--body")
    p_risk.set_defaults(func=cmd_review_mark_risk)
    p_policy_finding = review_sub.add_parser("attach-policy-finding")
    p_policy_finding.add_argument("review")
    p_policy_finding.add_argument("--body")
    p_policy_finding.set_defaults(func=cmd_review_attach_policy_finding)
    p_check_summary = review_sub.add_parser("attach-check-summary")
    p_check_summary.add_argument("review")
    p_check_summary.add_argument("--body")
    p_check_summary.set_defaults(func=cmd_review_attach_check_summary)
    p_close = review_sub.add_parser("close")
    p_close.add_argument("review")
    p_close.add_argument("--body")
    p_close.set_defaults(func=cmd_review_close)
    p_agent_pass = review_sub.add_parser("request-agent-pass")
    p_agent_pass.add_argument("review")
    p_agent_pass.add_argument("--task", required=True)
    p_agent_pass.add_argument("--assignee")
    p_agent_pass.set_defaults(func=cmd_review_request_agent_pass)

    p = sub.add_parser("advisory")
    advisory_sub = p.add_subparsers(dest="advisory_cmd", required=True)
    p_create = advisory_sub.add_parser("create")
    p_create.add_argument("project")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body")
    p_create.add_argument("--body-file")
    p_create.add_argument("--embargoed", action="store_true")
    p_create.add_argument("--affected-package", action="append")
    p_create.add_argument("--trusted-reviewer", action="append")
    p_create.set_defaults(func=cmd_advisory_create)
    p_list = advisory_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.set_defaults(func=cmd_advisory_list)
    p_inspect = advisory_sub.add_parser("inspect")
    p_inspect.add_argument("advisory")
    p_inspect.set_defaults(func=cmd_advisory_inspect)

    p = sub.add_parser("release")
    release_sub = p.add_subparsers(dest="release_cmd", required=True)
    p_prepare = release_sub.add_parser("prepare")
    p_prepare.add_argument("project")
    p_prepare.add_argument("--include", action="append", required=True)
    p_prepare.add_argument("--disclose", action="append")
    p_prepare.add_argument("--package", action="append")
    p_prepare.add_argument("--rollback-target")
    p_prepare.set_defaults(func=cmd_release_prepare)
    p_list = release_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.set_defaults(func=cmd_release_list)
    p_inspect = release_sub.add_parser("inspect")
    p_inspect.add_argument("release")
    p_inspect.set_defaults(func=cmd_release_inspect)

    p = sub.add_parser("policy")
    policy_sub = p.add_subparsers(dest="policy_cmd", required=True)
    p_eval = policy_sub.add_parser("evaluate")
    eval_sub = p_eval.add_subparsers(dest="evaluate_cmd", required=True)
    p_export_eval = eval_sub.add_parser("export")
    p_export_eval.add_argument("changeset")
    p_export_eval.add_argument("--override-reason")
    p_export_eval.set_defaults(func=cmd_policy_evaluate_export)
    p_access_eval = eval_sub.add_parser("access")
    p_access_eval.add_argument("object")
    p_access_eval.add_argument("--action")
    p_access_eval.set_defaults(func=cmd_policy_evaluate_access)
    p_explain = policy_sub.add_parser("explain")
    p_explain.add_argument("decision")
    p_explain.set_defaults(func=cmd_policy_explain)
    p_create_embargo = policy_sub.add_parser("create-embargo")
    p_create_embargo.add_argument("project")
    p_create_embargo.add_argument("--title", required=True)
    p_create_embargo.add_argument("--body")
    p_create_embargo.add_argument("--body-file")
    p_create_embargo.add_argument("--affected-package", action="append")
    p_create_embargo.add_argument("--trusted-reviewer", action="append")
    p_create_embargo.set_defaults(func=cmd_policy_create_embargo)
    p_disclose_embargo = policy_sub.add_parser("disclose-embargo")
    p_disclose_embargo.add_argument("project")
    p_disclose_embargo.add_argument("--advisory", dest="disclose", action="append", required=True)
    p_disclose_embargo.add_argument("--include", action="append", required=True)
    p_disclose_embargo.add_argument("--package", action="append")
    p_disclose_embargo.add_argument("--rollback-target")
    p_disclose_embargo.set_defaults(func=cmd_policy_disclose_embargo)
    p_visibility = policy_sub.add_parser("visibility")
    p_visibility.add_argument("object")
    p_visibility.set_defaults(func=cmd_policy_visibility)
    p_grant = policy_sub.add_parser("grant")
    p_grant.add_argument("object")
    p_grant.add_argument("--principal", required=True)
    p_grant.add_argument("--action", action="append")
    p_grant.set_defaults(func=cmd_policy_grant)
    p_grant_inspect = policy_sub.add_parser("grant-inspect")
    p_grant_inspect.add_argument("grant")
    p_grant_inspect.set_defaults(func=cmd_policy_grant_inspect)
    p_revoke = policy_sub.add_parser("revoke")
    p_revoke.add_argument("grant")
    p_revoke.set_defaults(func=cmd_policy_revoke)

    p = sub.add_parser("events")
    events_sub = p.add_subparsers(dest="events_cmd", required=True)
    p_list = events_sub.add_parser("list")
    p_list.add_argument("--workspace")
    p_list.add_argument("--project")
    p_list.add_argument("--kind")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_events_list)
    p_inspect = events_sub.add_parser("inspect")
    p_inspect.add_argument("event")
    p_inspect.set_defaults(func=cmd_events_inspect)

    p = sub.add_parser("artifacts")
    artifacts_sub = p.add_subparsers(dest="artifacts_cmd", required=True)
    p_list = artifacts_sub.add_parser("list")
    p_list.add_argument("--kind")
    p_list.add_argument("--label")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_artifacts_list)
    p_inspect = artifacts_sub.add_parser("inspect")
    p_inspect.add_argument("artifact")
    p_inspect.set_defaults(func=cmd_artifacts_inspect)
    p_read = artifacts_sub.add_parser("read")
    p_read.add_argument("artifact")
    p_read.set_defaults(func=cmd_artifacts_read)

    p = sub.add_parser("export")
    export_sub = p.add_subparsers(dest="export_cmd", required=True)
    p_list = export_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.add_argument("--changeset")
    p_list.add_argument("--kind")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_export_list)
    p_inspect = export_sub.add_parser("inspect")
    p_inspect.add_argument("export")
    p_inspect.set_defaults(func=cmd_export_inspect)
    p_patch = export_sub.add_parser("git-patch")
    p_patch.add_argument("changeset")
    p_patch.add_argument("--output")
    p_patch.add_argument("--override-reason")
    p_patch.add_argument("--summary", action="store_true")
    p_patch.set_defaults(func=cmd_export_patch)
    p_commit = export_sub.add_parser("git-commit")
    p_commit.add_argument("changeset")
    p_commit.add_argument("--repo")
    p_commit.add_argument("--message")
    p_commit.add_argument("--override-reason")
    p_commit.set_defaults(func=cmd_export_commit)
    p_pr = export_sub.add_parser("github-pr")
    p_pr.add_argument("changeset")
    p_pr.add_argument("--repo", required=True)
    p_pr.add_argument("--branch", required=True)
    p_pr.add_argument("--base")
    p_pr.add_argument("--title")
    p_pr.add_argument("--body")
    p_pr.add_argument("--draft", action="store_true")
    p_pr.add_argument("--create", action="store_true")
    p_pr.add_argument("--override-reason")
    p_pr.set_defaults(func=cmd_export_github_pr)

    p = sub.add_parser("config")
    config_sub = p.add_subparsers(dest="config_cmd", required=True)
    p_mat = config_sub.add_parser("materialize")
    p_mat.add_argument("workspace")
    p_mat.add_argument("--profile", required=True)
    p_mat.set_defaults(func=cmd_config_materialize)

    p = sub.add_parser("secrets")
    secrets_sub = p.add_subparsers(dest="secrets_cmd", required=True)
    p_put = secrets_sub.add_parser("put")
    p_put.add_argument("project")
    p_put.add_argument("name")
    p_put.add_argument("--value-file", required=True)
    p_put.add_argument("--scope", action="append")
    p_put.set_defaults(func=cmd_secrets_put)
    p_list = secrets_sub.add_parser("list")
    p_list.add_argument("project")
    p_list.set_defaults(func=cmd_secrets_list)
    p_inspect = secrets_sub.add_parser("inspect")
    p_inspect.add_argument("secret")
    p_inspect.add_argument("--project")
    p_inspect.set_defaults(func=cmd_secrets_inspect)
    p_lease = secrets_sub.add_parser("request-lease")
    p_lease.add_argument("workspace")
    p_lease.add_argument("secret")
    p_lease.add_argument("--env", required=True)
    p_lease.add_argument("--ttl-seconds", type=int)
    p_lease.set_defaults(func=cmd_secrets_request_lease)
    p_revoke = secrets_sub.add_parser("revoke")
    p_revoke.add_argument("lease")
    p_revoke.set_defaults(func=cmd_secrets_revoke)
    p_redact = secrets_sub.add_parser("redact-logs")
    p_redact.add_argument("check")
    p_redact.add_argument("--lease", action="append")
    p_redact.set_defaults(func=cmd_secrets_redact_logs)
    p_audit = secrets_sub.add_parser("audit")
    p_audit.add_argument("--workspace")
    p_audit.add_argument("--project")
    p_audit.add_argument("--secret")
    p_audit.add_argument("--limit", type=int, default=50)
    p_audit.set_defaults(func=cmd_secrets_audit)
    return parser


def get_parser() -> argparse.ArgumentParser:
    global PARSER_CACHE
    if PARSER_CACHE is None:
        PARSER_CACHE = build_parser()
    return PARSER_CACHE


# Commands that start servers or manage credentials never run on behalf of a remote client.
API_BLOCKED_COMMANDS = frozenset({"serve", "mcp", "auth"})


def raise_actor_mismatch(claimed: str | None, authenticated: str) -> None:
    raise TriError(
        "actor_mismatch",
        "This client is authenticated as a different actor.",
        object=claimed,
        authenticated_actor=authenticated,
    )


def normalize_actor_argv(argv: list[str], *, actor: str | None = None) -> tuple[list[str], str | None]:
    normalized: list[str] = []
    selected_actor = actor
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "--":
            normalized.extend(argv[i:])
            break
        if item == "--actor":
            if i + 1 >= len(argv) or argv[i + 1] == "--":
                raise TriError("invalid_command", "--actor requires a value.")
            selected_actor = argv[i + 1]
            i += 2
            continue
        if item.startswith("--actor="):
            selected_actor = item.split("=", 1)[1]
            if not selected_actor:
                raise TriError("invalid_command", "--actor requires a value.")
            i += 1
            continue
        normalized.append(item)
        i += 1
    if selected_actor:
        return ["--actor", selected_actor, *normalized], selected_actor
    return normalized, None


def execute_argv(
    argv: list[str],
    *,
    actor: str | None = None,
    start: float | None = None,
    store: Store | None = None,
    pack_on_close: bool | None = None,
    pinned_actor: str | None = None,
    server_paths_allowed: bool = True,
    forward_remote: bool = True,
) -> tuple[dict[str, Any], int]:
    """Run one Tri command. pinned_actor is an authenticated identity: the command
    runs as that actor and any other --actor in argv is refused. With
    server_paths_allowed=False (HTTP API callers), arguments that name files or
    directories on the server's disk are refused for non-maintainers."""
    start = time.time() if start is None else start
    argv = list(argv)
    argv, remote = split_remote_flag(argv)
    if store is None and forward_remote:
        remote = remote or os.environ.get("TRI_REMOTE", "").strip() or None
        if remote:
            from tri.remote import LOCAL_ONLY_COMMANDS, command_words, dispatch

            words = command_words(argv)
            if words and words[0] not in LOCAL_ONLY_COMMANDS:
                output = dispatch(argv, remote.rstrip("/"))
                return output, 0 if output.get("ok", False) else 1
    if pinned_actor is not None:
        actor = pinned_actor
    default_actor = actor or os.environ.get("TRI_ACTOR", "actor_local")
    try:
        parse_argv, parsed_actor = normalize_actor_argv(argv, actor=actor)
        if pinned_actor is not None and parsed_actor != pinned_actor:
            raise_actor_mismatch(parsed_actor, pinned_actor)
        effective_actor = parsed_actor or default_actor
        require_known_actor(effective_actor)
    except TriError as exc:
        return (
            envelope(
                "tri.error",
                ["tri", *argv],
                ok=False,
                actor=locals().get("effective_actor", default_actor),
                diagnostics=[exc.diagnostic],
                start=start,
            ),
            1,
        )
    parser = get_parser()
    try:
        args = parser.parse_args(parse_argv)
    except TriError as exc:
        return (
            envelope(
                "tri.error",
                ["tri", *argv],
                ok=False,
                actor=parsed_actor or default_actor,
                diagnostics=[exc.diagnostic],
                start=start,
            ),
            1,
        )
    if not server_paths_allowed:
        try:
            require_no_server_paths(args, actor=getattr(args, "actor", effective_actor))
        except TriError as exc:
            return envelope("tri.error", ["tri", *argv], ok=False, actor=getattr(args, "actor", effective_actor), diagnostics=[exc.diagnostic], start=start), 1
    active_store = store or Store(pack_on_close=pack_on_close)
    try:
        output = args.func(active_store, args, ["tri", *parse_argv], start)
        return output, 0 if output.get("ok", False) else 1
    except TriError as exc:
        return (
            envelope(
                "tri.error",
                ["tri", *argv],
                ok=False,
                actor=getattr(args, "actor", "actor_local"),
                diagnostics=[exc.diagnostic],
                start=start,
            ),
            1,
        )
    finally:
        if store is None:
            try:
                maybe_auto_reap(active_store)
            except Exception:
                pass
            active_store.close()


def main(argv: list[str] | None = None) -> None:
    output, status = execute_argv(list(sys.argv[1:] if argv is None else argv), start=time.time())
    if output["schema"] not in ("tri.api.server", "tri.mcp.server"):
        print_json(output)
    if status:
        raise SystemExit(status)


if __name__ == "__main__":
    main()
