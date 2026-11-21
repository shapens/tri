"""Leak sweep: nothing private escapes through any read route.

One fixture project holds, at once, every kind of thing an unprivileged actor
must not learn about:

- a hidden path: a private package boundary (``vault/**``) inside public work,
- a secret-like path (``*credentials*``) and a stored secret with its value,
- another actor's private workspace and changeset, plus a private changeset
  inside a public workspace,
- an embargoed advisory with its private fix workspace and changeset.

Each carries a unique canary string (file contents, file names, titles, bodies,
secret names and values). The ids of the private objects are canaries as well.
An unprivileged actor (``agent:outsider``) then runs every read-shaped command,
with every flag that changes the output, through the CLI, ``tri batch``, and the
authenticated HTTP API. No canary may appear anywhere in the serialized response.

The one allowance: a value the actor already knows may come back. That is a
value its request carries (an id or path it passed in), or a private id it sent
in an earlier request (its own policy.denied events name it). Content canaries
never carry over, and searches query a fragment of a canary, never the canary.

Denials are followed too: each policy_denied decision is explained as the same
actor, and the explanation must not name what the denial withheld.

Decisions recorded here (tri.md, "Policy Applies To Reads And Exports" and
"Hidden In-Flight Work"):

- A hidden path is hidden by name too. Reads may say that something was
  redacted (counts, ``path_hash``), but never the path, its blob or tree id, or
  the package boundary that hides it.
- Tasks are public objects in the current model (they carry no visibility), so
  task titles are not canaries; private attempts on a task are.
- Ids that public readers are meant to see stay public: a lease id in a public
  workspace (it is the redaction marker for the secret it carries), and events
  in a public workspace that concern a private changeset there (shown with that
  ref redacted). The secret and the changeset stay private.
- A check's command line and output are written by whoever ran it. The fixture
  never puts hidden content into a check a public reader can see: that would be
  the runner publishing it, not Tri leaking it. Secret values are different:
  Tri redacts leased secrets from logs, and the sweep checks that.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402

try:  # run through `discover -s tests`, or as tests.test_leak_sweep
    from conformance.harness import server_argv  # noqa: E402
except ImportError:  # pragma: no cover
    from tests.conformance.harness import server_argv  # noqa: E402

OUTSIDER = "agent:outsider"
OWNER = "agent:owner"
SECURITY = "human:security"
ADMIN = "actor_local"

# Canaries. Each has a distinct middle word so a search can ask for the word
# without the request itself containing the canary.
C = {
    "hidden_name": "lkc_hiddenname_q7z",
    "hidden_content": "lkc_hiddencontent_q7z",
    "secretlike_name": "lkc_secretlikename_q7z",
    "secretlike_content": "lkc_secretlikecontent_q7z",
    "secret_name": "LKC_SECRETNAME_Q7Z",
    "secret_value": "lkc_secretvalue_q7z",
    "private_name": "lkc_privatename_q7z",
    "private_content": "lkc_privatecontent_q7z",
    "private_title": "lkc_privatetitle_q7z",
    "private_check_output": "lkc_privatecheckout_q7z",
    "private_review": "lkc_privatereview_q7z",
    "private_mount": "lkc_privatemount_q7z",
    "privcs_title": "lkc_privcstitle_q7z",
    "advisory_title": "lkc_advisorytitle_q7z",
    "advisory_body": "lkc_advisorybody_q7z",
    "embargo_name": "lkc_embargoname_q7z",
    "embargo_content": "lkc_embargocontent_q7z",
    "embargo_review": "lkc_embargoreview_q7z",
}
SEARCH_WORDS = sorted({value.split("_")[1].lower() for value in C.values()})

HIDDEN_PATH = f"vault/{C['hidden_name']}.txt"
SECRETLIKE_PATH = f"config/{C['secretlike_name']}_credentials.json"
PRIVATE_PATH = f"src/{C['private_name']}.txt"
EMBARGO_PATH = f"src/{C['embargo_name']}.txt"
PROBE_PATHS = ["src/app.txt", "README.md", HIDDEN_PATH, "vault", SECRETLIKE_PATH, PRIVATE_PATH, EMBARGO_PATH, ".env"]

# Contract names whose verbs make them reads. A command matching any of these
# must be swept or listed in READ_EXEMPTIONS.
READ_VERBS = (
    "list",
    "inspect",
    "read",
    "search",
    "log",
    "blame",
    "diff",
    "stream",
    "compare",
    "explain",
    "evaluate",
    "visibility",
    "audit",
    "attempts",
    "artifacts",
    "merge-base",
    "stat",
    "doctor",
    "commands",
    "schema",
    "fetch",
    "jobs",
)

# Read-shaped commands the sweep does not run, and why.
READ_EXEMPTIONS = {
    # Reads the operator's local token file named on the command line, not the
    # Tri store, and prints only token ids, labels, and actor names (never the
    # token). The API and batch refuse it, so a remote actor cannot reach it.
    "auth-list": "local operator file, refused by the API and batch",
}


def is_read_shaped(name: str) -> bool:
    if name.startswith("export-") or name.endswith("-leases"):
        return True
    return any(re.search(rf"(^|-){re.escape(verb)}($|-)", name) for verb in READ_VERBS)


def contract_name_for_argv(argv: list[str]) -> str | None:
    names = {contract["name"] for contract in cli.COMMAND_CONTRACTS}
    words = [item for item in argv if not item.startswith("--")]
    for size in (3, 2, 1):
        candidate = "-".join(words[:size])
        if candidate in names:
            return candidate
    return None


class LeakSweepFixture:
    """Builds the fixture project in-process and records every private id."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.export_repo = root / "export-repo"
        self.ids: dict[str, Any] = {}
        self.private_ids: set[str] = set()
        self.public_ids: set[str] = set()

    # Helpers ------------------------------------------------------------------

    def tri(self, *argv: str, actor: str = ADMIN, ok: bool | None = True) -> dict:
        output, _ = cli.execute_argv(["--actor", actor, *argv])
        if ok is True and not output["ok"]:
            raise AssertionError(f"fixture step failed: tri {' '.join(argv)} as {actor}\n{json.dumps(output['diagnostics'], indent=1)}")
        return output

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def make_repo(self, repo: Path) -> None:
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.email", "tri@example.com")
        self.git(repo, "config", "user.name", "Tri Test")
        (repo / "src").mkdir()
        (repo / "src" / "app.txt").write_text("public line\n")
        (repo / "README.md").write_text("hello\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "initial")

    def check(self, workspace: str, code: str, *, actor: str = ADMIN, lease: str | None = None) -> dict:
        argv = ["run", workspace]
        if lease:
            argv += ["--with-lease", lease]
        return self.tri(*argv, "--", sys.executable, "-c", code, actor=actor)["data"]

    def approve(self, changeset: str, *, actor: str = ADMIN, reviewers: str | None = None) -> str:
        argv = ["review", "create", changeset] + (["--reviewers", reviewers] if reviewers else [])
        review = self.tri(*argv, actor=actor)["data"]["id"]
        self.tri("review", "approve", review, actor=actor)
        return review

    # Build --------------------------------------------------------------------

    def build(self) -> None:
        self.make_repo(self.repo)
        self.make_repo(self.export_repo)
        ids = self.ids
        imported = self.tri("import", str(self.repo), "--name", "app")["data"]
        ids["project"] = imported["project"]["id"] if isinstance(imported["project"], dict) else imported["project"]
        ids["target"] = imported["target"]["id"]
        ids["base_snapshot"] = imported["target"]["snapshot"]
        ids["vault_package"] = self.tri("packages", "declare", "app", "vault", "--path", "vault/**", "--visibility", "private")["data"]["id"]
        self.tri("packages", "declare", "app", "web", "--path", "src/**", "--visibility", "public")
        self.tri("checks", "set-create", "app", "smoke", "--required-check", "python3 -c pass")
        self.tri("merge-rules", "add", "app", "docs", "--path", "docs/*")
        self.tri("ignore", "add", "app", "*.log")
        ids["task"] = self.tri("task", "create", "app", "--title", "Leak sweep task", "--body", "public task")["data"]["id"]

        # Another actor's private workspace, created before main moves so a
        # conflict can be recorded against it.
        priv = self.tri("workspace", "create", "app", "--from", "main", "--task", ids["task"], actor=OWNER)["data"]
        ids["ws_priv"] = priv["id"]
        ids["run_priv"] = priv["task_run"]
        self.tri("files", "write", priv["id"], "src/app.txt", "--text", f"{C['private_content']}\n", actor=OWNER)
        self.tri("files", "write", priv["id"], PRIVATE_PATH, "--text", f"{C['private_content']} file\n", actor=OWNER)
        ids["check_priv"] = self.check(priv["id"], f"print('{C['private_check_output']}')", actor=OWNER)["id"]
        ids["cs_priv"] = self.tri("changeset", "create", priv["id"], "--title", C["private_title"], actor=OWNER)["data"]["id"]
        ids["review_priv"] = self.tri("review", "create", ids["cs_priv"], actor=OWNER)["data"]["id"]
        self.tri("review", "comment", ids["review_priv"], "--body", C["private_review"], "--path", PRIVATE_PATH, "--line", "1", actor=OWNER)
        mount = self.root / f"mnt-{C['private_mount']}"
        ids["lease_mount_priv"] = self.tri("workspace", "mount", priv["id"], str(mount), actor=OWNER)["data"]["lease"]
        self.tri("policy", "grant", priv["id"], "--principal", f"actor:{SECURITY}", "--action", "workspace.read")

        # Public work that touches the hidden package path, merged to main so
        # history, blame, and target reads all cover the hidden path.
        pub = self.tri("workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]
        ids["ws_pub"] = pub["id"]
        self.tri("files", "write", pub["id"], "src/app.txt", "--text", "public edit\n")
        self.tri("files", "write", pub["id"], HIDDEN_PATH, "--text", f"{C['hidden_content']}\n")
        ids["check_pub"] = self.check(pub["id"], "print('public check ok')")["id"]
        ids["cs_pub"] = self.tri("changeset", "create", pub["id"], "--title", "Public change", "--visibility", "public")["data"]["id"]
        ids["review_pub"] = self.tri("review", "create", ids["cs_pub"])["data"]["id"]
        self.tri("review", "comment", ids["review_pub"], "--body", "see the vault file", "--path", HIDDEN_PATH, "--line", "1")
        self.tri("review", "approve", ids["review_pub"])
        self.tri("export", "git-patch", ids["cs_pub"])
        self.tri("export", "git-commit", ids["cs_pub"], "--repo", str(self.export_repo))
        self.tri("changeset", "merge", ids["cs_pub"], "--target", "main")
        ids["release_pub"] = self.tri("release", "prepare", "app", "--include", ids["cs_pub"], "--package", "web")["data"]["id"]

        # The private workspace now conflicts with main on src/app.txt.
        self.tri("workspace", "update", priv["id"], "--from", "main", actor=OWNER, ok=None)

        # Public work with a secret-like path, a leased secret, and a private
        # changeset inside the public workspace.
        secret_file = self.root / "secret.txt"
        secret_file.write_text(C["secret_value"])
        ids["secret"] = self.tri("secrets", "put", "app", C["secret_name"], "--value-file", str(secret_file))["data"]["id"]
        self.tri("policy", "grant", ids["secret"], "--principal", f"actor:{OWNER}", "--action", "secret.use")
        mix = self.tri("workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]
        ids["ws_mix"] = mix["id"]
        self.tri("files", "write", mix["id"], SECRETLIKE_PATH, "--text", f"{C['secretlike_content']}\n")
        self.tri("files", "write", mix["id"], "src/mix.txt", "--text", "mixed public\n")
        ids["lease_secret_mix"] = self.tri("secrets", "request-lease", mix["id"], C["secret_name"], "--env", "API_TOKEN")["data"]["id"]
        ids["check_mix"] = self.check(mix["id"], "import os; print('token', os.environ['API_TOKEN'])", lease=ids["lease_secret_mix"])["id"]
        ids["cs_mix"] = self.tri("changeset", "create", mix["id"], "--title", "Mixed public change", "--visibility", "public")["data"]["id"]
        self.approve(ids["cs_mix"])
        self.tri("export", "git-patch", ids["cs_mix"], ok=None)
        # The private changeset's content stays in a public workspace, so only
        # its title and its own checks are private, not the file content.
        self.tri("files", "write", mix["id"], "src/mix.txt", "--text", "mixed public, second pass\n")
        ids["cs_mixpriv"] = self.tri("changeset", "create", mix["id"], "--title", C["privcs_title"], "--visibility", "private")["data"]["id"]
        ids["check_mixpriv"] = self.tri("changeset", "revalidate", ids["cs_mixpriv"])["data"]["checks"][0]["id"]
        ids["lease_secret_priv"] = self.tri("secrets", "request-lease", priv["id"], C["secret_name"], "--env", "API_TOKEN", actor=OWNER)["data"]["id"]

        # Embargoed advisory with its private fix.
        advisory = self.tri(
            "policy", "create-embargo", "app", "--title", C["advisory_title"], "--body", C["advisory_body"],
            "--affected-package", "web", "--trusted-reviewer", SECURITY,
        )["data"]
        ids["advisory"] = advisory["id"]
        emb = self.tri("workspace", "create", "app", "--from", "main", "--advisory", advisory["id"])["data"]
        ids["ws_emb"] = emb["id"]
        self.tri("files", "write", emb["id"], EMBARGO_PATH, "--text", f"{C['embargo_content']}\n")
        ids["check_emb"] = self.check(emb["id"], "print('embargo check')")["id"]
        ids["cs_emb"] = self.tri("changeset", "create", emb["id"], "--visibility", "embargoed")["data"]["id"]
        ids["review_emb"] = self.tri("review", "create", ids["cs_emb"], "--reviewers", SECURITY)["data"]["id"]
        self.tri("review", "comment", ids["review_emb"], "--body", C["embargo_review"])
        self.tri("review", "approve", ids["review_emb"])
        self.tri("export", "git-patch", ids["cs_emb"], ok=None)
        self.tri("release", "prepare", "app", "--include", ids["cs_emb"], ok=None)
        # Defined last: an environment recipe runs before every later check.
        self.tri("env", "define", "app", "deps", "--install", "true", "--output", "node_modules")

        self.collect()

    # Id collection ------------------------------------------------------------

    def collect(self) -> None:
        ids = self.ids
        private_workspaces = {ids["ws_priv"], ids["ws_emb"]}
        public_workspaces = {ids["ws_pub"], ids["ws_mix"]}
        private_changesets = {ids["cs_priv"], ids["cs_emb"], ids["cs_mixpriv"]}
        public_changesets = {ids["cs_pub"], ids["cs_mix"]}
        self.private_ids |= private_workspaces | private_changesets
        self.private_ids |= {
            ids["vault_package"], ids["secret"], ids["advisory"], ids["run_priv"],
            ids["review_priv"], ids["review_emb"], ids["check_priv"], ids["check_emb"], ids["check_mixpriv"],
            ids["lease_secret_priv"], ids["lease_mount_priv"],
        }
        # A lease id in a public workspace is public: workspace readers see it in
        # check records and in the redaction marker that replaces the value. The
        # secret it points at (id, name, value) stays private.
        self.public_ids.add(ids["lease_secret_mix"])
        self.public_ids |= public_workspaces | public_changesets | {ids["review_pub"], ids["check_pub"], ids["check_mix"], ids["task"], ids["release_pub"]}

        ids["workspaces"] = sorted(private_workspaces | public_workspaces)
        ids["changesets"] = sorted(private_changesets | public_changesets)
        ids["reviews"] = [ids["review_pub"], ids["review_priv"], ids["review_emb"]]
        ids["checks"] = [ids["check_pub"], ids["check_mix"], ids["check_priv"], ids["check_emb"], ids["check_mixpriv"]]
        ids["leases"] = [ids["lease_secret_mix"], ids["lease_secret_priv"], ids["lease_mount_priv"]]

        # Operations and the snapshots they produced.
        operations: list[str] = []
        snapshots_private: set[str] = set()
        snapshots_public: set[str] = {ids["base_snapshot"]}
        for ref in ids["workspaces"]:
            for op in self.tri("operations", "list", ref)["data"]["operations"]:
                operations.append(op["id"])
                produced = {s for s in (op.get("output_snapshot"), op.get("input_snapshot")) if s}
                if ref in private_workspaces:
                    self.private_ids.add(op["id"])
                    snapshots_private |= produced
                else:
                    snapshots_public |= produced
        for target in self.tri("targets", "list", "app")["data"]["targets"]:
            snapshots_public.add(target["snapshot"])
        ids["main_snapshot"] = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        snapshots_private -= snapshots_public
        self.private_ids |= snapshots_private
        ids["operations"] = sorted(set(operations))
        ids["snapshots"] = sorted(snapshots_private | snapshots_public)
        ids["head_priv"] = self.tri("workspace", "inspect", ids["ws_priv"])["data"]["head_snapshot"]
        ids["head_emb"] = self.tri("workspace", "inspect", ids["ws_emb"])["data"]["head_snapshot"]
        ids["head_pub"] = self.tri("workspace", "inspect", ids["ws_pub"])["data"]["head_snapshot"]

        # Trees and blobs: the hidden path's blob and subtree are private; so is
        # everything only private snapshots hold.
        trees: set[str] = set()
        blobs: set[str] = set()
        for snapshot in ids["snapshots"]:
            root_tree = self.tri("snapshots", "inspect", snapshot)["data"]["root_tree"]
            trees.add(root_tree)
            if snapshot in snapshots_private:
                self.private_ids.add(root_tree)
        vault_tree = self.vault_tree(ids["main_snapshot"])
        if vault_tree:
            trees.add(vault_tree)
            self.private_ids.add(vault_tree)
        for ref, path, private in (
            (ids["main_snapshot"], HIDDEN_PATH, True),
            (ids["ws_priv"], PRIVATE_PATH, True),
            (ids["ws_priv"], "src/app.txt", True),
            (ids["ws_emb"], EMBARGO_PATH, True),
            (ids["ws_mix"], "src/mix.txt", False),
            (ids["main_snapshot"], "src/app.txt", False),
        ):
            blob = self.tri("files", "stat", ref, path)["data"].get("blob")
            if blob:
                blobs.add(blob)
                (self.private_ids if private else self.public_ids).add(blob)
        ids["trees"] = sorted(trees)
        ids["blobs"] = sorted(blobs)

        # Review actions.
        actions: list[str] = []
        for review in ids["reviews"]:
            for action in self.tri("review", "inspect", review)["data"]["actions"]:
                actions.append(action["id"])
                if review != ids["review_pub"]:
                    self.private_ids.add(action["id"])
        ids["review_actions"] = actions

        # Artifacts: the logs of private checks are private.
        artifacts: set[str] = set()
        for check in ids["checks"]:
            for artifact in self.tri("checks", "artifacts", check)["data"]["artifacts"]:
                artifact_id = artifact["id"] if isinstance(artifact, dict) else artifact
                artifacts.add(artifact_id)
                if check in self.private_ids:
                    self.private_ids.add(artifact_id)
        for artifact in self.tri("artifacts", "list", "--limit", "500")["data"]["artifacts"]:
            artifacts.add(artifact["id"])
        ids["artifacts"] = sorted(artifacts)

        # Exports, releases, conflicts, grants.
        exports = self.tri("export", "list", "app", "--limit", "500")["data"]["exports"]
        ids["exports"] = [item["id"] for item in exports]
        for item in exports:
            if item["changeset"] in private_changesets:
                self.private_ids.add(item["id"])
        ids["releases"] = [item["id"] for item in self.tri("release", "list", "app")["data"]["releases"]]
        conflicts = self.tri("conflicts", "list", "--workspace", ids["ws_priv"])["data"]["conflicts"]
        ids["conflicts"] = [item["id"] for item in conflicts]
        self.private_ids |= set(ids["conflicts"])
        grants = self.tri("policy", "visibility", ids["ws_priv"])["data"].get("grants", [])
        ids["grants"] = [grant["id"] for grant in grants if isinstance(grant, dict) and "id" in grant]
        secret_grants = self.tri("policy", "visibility", ids["secret"])["data"].get("grants", [])
        ids["grants"] += [grant["id"] for grant in secret_grants if isinstance(grant, dict) and "id" in grant]
        self.private_ids |= set(ids["grants"])

        # Events and policy decisions about private objects.
        events = self.tri("events", "list", "--limit", "500")["data"]["events"]
        ids["events"] = []
        ids["decisions"] = []
        for event in events:
            ids["events"].append(event["id"])
            refs = event.get("object_refs") or {}
            # An event in a public workspace is public even when it concerns a
            # private changeset there: readers see it with that ref redacted.
            private_event = event.get("workspace") in private_workspaces or (
                event.get("workspace") is None
                and any(refs.get(key) in self.private_ids for key in ("changeset", "review", "check", "advisory", "conflict", "lease"))
            )
            if private_event:
                self.private_ids.add(event["id"])
            decision = (event.get("policy_result") or {}).get("id") or (event.get("object_refs") or {}).get("decision")
            if isinstance(decision, str) and decision.startswith("decision_"):
                ids["decisions"].append(decision)
                if private_event:
                    self.private_ids.add(decision)

    def vault_tree(self, snapshot: str) -> str | None:
        root_tree = self.tri("snapshots", "inspect", snapshot)["data"]["root_tree"]
        data = self.tri("trees", "inspect", root_tree)["data"]
        for entry in data.get("entries", []):
            if isinstance(entry, dict) and entry.get("path") in ("vault", "vault/") and entry.get("tree"):
                return entry["tree"]
            if isinstance(entry, dict) and entry.get("name") == "vault" and entry.get("id", "").startswith("tree_"):
                return entry["id"]
        return None


def sweep_argvs(fx: LeakSweepFixture) -> list[list[str]]:
    """Every read-shaped request the outsider makes, built from the fixture ids."""
    ids = fx.ids
    refs = [ids["target"], *ids["workspaces"], ids["main_snapshot"], ids["head_priv"], ids["head_emb"], ids["head_pub"]]
    argvs: list[list[str]] = [
        ["commands"],
        ["schema"],
        ["schema", "tri.changeset.inspect"],
        ["doctor"],
        ["projects", "list"],
        ["projects", "inspect", "app"],
        ["packages", "list", "app"],
        ["packages", "inspect", "app", "vault"],
        ["packages", "inspect", "app", "web"],
        ["packages", "inspect", ids["vault_package"]],
        ["targets", "list", "app"],
        ["targets", "inspect", "app", "main"],
        ["targets", "inspect", ids["target"]],
        ["snapshots", "list", "app"],
        ["env", "list", "app"],
        ["env", "inspect", "app", "deps"],
        ["ignore", "list", "app"],
        ["checks", "set-list", "app"],
        ["checks", "set-inspect", "app", "smoke"],
        ["mirror", "list"],
        ["mirror", "list", "app", "--all"],
        ["mirror", "inspect", "app"],
        ["verify", "list", "app"],
        ["verify", "list", "app", "--status", "pending"],
        ["verify", "inspect", "verify_missing"],
        ["runner", "jobs"],
        ["runner", "fetch", "job_missing", "--path", "src/app.txt"],
        ["merge-rules", "list", "app"],
        ["merge-rules", "list", "app", "--all"],
        ["merge-rules", "inspect", "app", "docs"],
        ["workspace", "list"],
        ["workspace", "list", "app"],
        ["leases", "list"],
        ["leases", "list", "--status", "active"],
        ["conflicts", "list"],
        ["conflicts", "list", "--status", "open"],
        ["advisory", "list", "app"],
        ["release", "list", "app"],
        ["secrets", "list", "app"],
        ["secrets", "inspect", C["secret_name"], "--project", "app"],
        ["secrets", "audit"],
        ["secrets", "audit", "--project", "app"],
        ["secrets", "audit", "--secret", ids["secret"]],
        ["events", "list"],
        ["events", "list", "--project", "app", "--limit", "500"],
        ["events", "list", "--kind", "workspace.write_file", "--limit", "500"],
        ["events", "list", "--kind", "secret.lease_granted"],
        ["events", "list", "--kind", "policy.evaluated", "--limit", "500"],
        ["artifacts", "list", "--limit", "500"],
        ["artifacts", "list", "--kind", "stdout"],
        ["artifacts", "list", "--kind", "git_patch"],
        ["export", "list", "app", "--limit", "500"],
        ["export", "list", "app", "--kind", "git_patch"],
        ["actors", "inspect", OWNER],
        ["actors", "inspect", OUTSIDER],
        ["actors", "inspect", ADMIN],
        ["task", "inspect", ids["task"]],
        ["task", "attempts", ids["task"]],
        ["task", "run-inspect", ids["run_priv"]],
    ]
    for word in SEARCH_WORDS:
        argvs.append(["search", "app", word])
        argvs.append(["search", "app", word, "--include-history"])
        argvs.append(["search", "app", word, "--include-history", "--case-sensitive"])
        argvs.append(["search", "app", word, "--include-history", "--glob", "vault/**"])
        for scope in cli.SEARCH_SCOPES:
            argvs.append(["search", "app", word, "--scope", scope, "--include-history"])
        for ref in refs:
            argvs.append(["files", "search", ref, word])
        argvs.append(["files", "search", "main", word, "--glob", "vault/**"])
    for ref in refs:
        argvs.append(["files", "list", ref])
        for path in PROBE_PATHS:
            argvs.append(["files", "stat", ref, path])
            argvs.append(["files", "read", ref, path])
            argvs.append(["files", "log", ref, path])
            argvs.append(["files", "blame", ref, path])
        argvs.append(["files", "fetch", ref, *sum((["--path", path] for path in PROBE_PATHS), [])])
        argvs.append(["files", "fetch", ref, "--path", HIDDEN_PATH, "--hashes-only"])
        argvs.append(["files", "fetch", ref, "--path", "src/app.txt", "--hashes-only"])
        argvs.append(["files", "read", ref, HIDDEN_PATH, "--offset", "1", "--limit", "4"])
        argvs.append(["files", "log", ref, "src/app.txt", "--limit", "1"])
    for snapshot in ids["snapshots"]:
        argvs.append(["snapshots", "inspect", snapshot])
    for left, right in (
        (ids["main_snapshot"], ids["head_priv"]),
        (ids["ws_pub"], ids["ws_priv"]),
        (ids["ws_priv"], ids["ws_emb"]),
        ("main", ids["head_emb"]),
    ):
        argvs.append(["snapshots", "merge-base", left, right])
    for tree in ids["trees"]:
        argvs.append(["trees", "inspect", tree])
    for blob in ids["blobs"]:
        argvs.append(["blobs", "inspect", blob])
    for workspace in ids["workspaces"]:
        argvs += [
            ["workspace", "inspect", workspace],
            ["workspace", "list", "app"],
            ["workspace", "leases", workspace],
            ["leases", "list", "--workspace", workspace],
            ["salvage", "list", workspace],
            ["conflicts", "list", "--workspace", workspace],
            ["events", "list", "--workspace", workspace, "--limit", "500"],
            ["secrets", "audit", "--workspace", workspace],
        ]
    for ref in [*ids["workspaces"], *ids["changesets"]]:
        argvs += [
            ["operations", "list", ref],
            ["checks", "list", ref],
            ["diff", ref, "--path", "vault/**"],
            ["diff", ref, "--path", HIDDEN_PATH],
        ]
        for mode in ("summary", "files", "patch", "machine"):
            argvs.append(["diff", ref, "--mode", mode])
    for head in (ids["head_pub"], ids["head_priv"], ids["head_emb"], ids["main_snapshot"]):
        for mode in ("summary", "files", "patch", "machine"):
            argvs.append(["diff", ids["base_snapshot"], head, "--mode", mode])
    for changeset in ids["changesets"]:
        argvs += [
            ["changeset", "inspect", changeset],
            ["changeset", "split", changeset, "--paths", "vault/**"],
            ["changeset", "split", changeset, "--paths", "src/**"],
            ["policy", "evaluate", "export", changeset],
            ["export", "list", "app", "--changeset", changeset],
            ["export", "git-patch", changeset],
            ["export", "git-patch", changeset, "--summary"],
            ["export", "git-commit", changeset, "--repo", str(fx.export_repo)],
            ["export", "github-pr", changeset, "--repo", str(fx.export_repo), "--branch", f"leak-{changeset}"],
        ]
    argvs.append(["changeset", "compare", *ids["changesets"]])
    argvs.append(["changeset", "compare", ids["cs_pub"], ids["cs_priv"]])
    argvs.append(["checks", "compare", *ids["checks"]])
    argvs.append(["checks", "compare", ids["check_pub"], ids["check_priv"]])
    for check in ids["checks"]:
        argvs += [
            ["checks", "inspect", check],
            ["checks", "artifacts", check],
            ["checks", "stream", check],
            ["checks", "stream", check, "--log", "stderr"],
            ["checks", "stream", check, "--offset", "2", "--limit", "8"],
        ]
    for review in ids["reviews"]:
        argvs.append(["review", "inspect", review])
    for action in ids["review_actions"]:
        argvs.append(["review", "action-inspect", action])
    for operation in ids["operations"]:
        argvs.append(["operations", "inspect", operation])
    argvs.append(["advisory", "inspect", ids["advisory"]])
    for release in ids["releases"]:
        argvs.append(["release", "inspect", release])
    for conflict in ids["conflicts"]:
        argvs.append(["conflicts", "inspect", conflict])
    for lease in ids["leases"]:
        argvs += [["leases", "inspect", lease], ["workspace", "lease-inspect", lease]]
    for grant in ids["grants"]:
        argvs.append(["policy", "grant-inspect", grant])
    argvs.append(["secrets", "inspect", ids["secret"]])
    for artifact in ids["artifacts"]:
        argvs += [["artifacts", "inspect", artifact], ["artifacts", "read", artifact]]
    for export in ids["exports"]:
        argvs.append(["export", "inspect", export])
    for event in ids["events"]:
        argvs.append(["events", "inspect", event])
    for decision in ids["decisions"]:
        argvs.append(["policy", "explain", decision])
    every_object = sorted(fx.private_ids | fx.public_ids | set(ids["snapshots"]) | set(ids["trees"]))
    for object_id in every_object:
        argvs.append(["objects", "inspect", object_id])
        argvs.append(["policy", "visibility", object_id])
        argvs.append(["policy", "evaluate", "access", object_id])
    for workspace in ids["workspaces"]:
        argvs.append(["policy", "evaluate", "access", workspace, "--action", "workspace.write"])
    return argvs


class LeakSweepTests(unittest.TestCase):
    """Builds the fixture once per test; the HTTP test starts its own server."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-leak-")
        # Registered first so it runs last: servers registered later stop before
        # the directory goes. Windows cannot delete a SQLite file a server holds.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {"TRI_HOME": str(self.root / "tri-home")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        os.environ.pop("TRI_RESPONSE", None)
        self.fx = LeakSweepFixture(self.root)
        self.fx.build()
        self.canaries = {**{f"canary:{key}": value for key, value in C.items()}, **{f"private-id:{object_id}": object_id for object_id in self.fx.private_ids}}
        self.assertGreater(len(self.fx.private_ids), 30, "fixture should produce many private ids")
        self.known_ids: set[str] = set()

    # Leak detection -----------------------------------------------------------

    def leaks_in(self, request_text: str, response_text: str) -> list[str]:
        """Canaries in the response that the actor did not already know.

        The request's own values are known. So is a private id the actor sent in
        an earlier request: its own policy.denied events name the ids it asked
        for, and listing them back reveals nothing. Content canaries (names,
        titles, contents, secret values) never carry over between requests.
        """
        found = []
        for label, value in self.canaries.items():
            if value in request_text:
                continue
            if label.startswith("private-id:") and value in self.known_ids:
                continue
            if value in response_text:
                index = response_text.index(value)
                found.append(f"{label} …{response_text[max(0, index - 160):index + len(value) + 40]}…")
        self.known_ids |= {object_id for object_id in self.fx.private_ids if object_id in request_text}
        return found

    def outsider(self, argv: list[str]) -> tuple[dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            output, _ = cli.execute_argv(["--actor", OUTSIDER, *argv])
        return output, json.dumps(output) + out.getvalue() + err.getvalue()

    def assert_no_leaks(self, leaks: list[str], swept: int) -> None:
        self.assertGreater(swept, 0)
        if leaks:
            self.fail(f"{len(leaks)} leak(s) across {swept} requests:\n" + "\n".join(leaks[:80]))

    # Tests --------------------------------------------------------------------

    def test_fixture_is_readable_by_privileged_actors(self) -> None:
        # The canaries must really be in the store, or the sweep proves nothing.
        ids = self.fx.ids
        self.assertIn(C["hidden_content"], self.fx.tri("files", "read", ids["main_snapshot"], HIDDEN_PATH)["data"]["text"])
        self.assertIn(C["private_content"], self.fx.tri("files", "read", ids["ws_priv"], PRIVATE_PATH, actor=OWNER)["data"]["text"])
        self.assertEqual(self.fx.tri("changeset", "inspect", ids["cs_priv"], actor=OWNER)["data"]["title"], C["private_title"])
        self.assertEqual(self.fx.tri("advisory", "inspect", ids["advisory"])["data"]["title"], C["advisory_title"])
        self.assertIn(C["embargo_content"], self.fx.tri("files", "read", ids["ws_emb"], EMBARGO_PATH)["data"]["text"])
        self.assertEqual(self.fx.tri("secrets", "inspect", ids["secret"])["data"]["name"], C["secret_name"])
        self.assertTrue(self.fx.ids["conflicts"], "the private workspace should hold a conflict")
        patches = [self.fx.tri("artifacts", "read", artifact)["data"].get("text") or "" for artifact in ids["artifacts"]]
        self.assertTrue(any(C["hidden_content"] in text for text in patches), "the maintainer's export patch should hold the hidden file")
        # The outsider is refused the private objects themselves...
        for argv in (
            ["changeset", "inspect", ids["cs_priv"]],
            ["changeset", "inspect", ids["cs_mixpriv"]],
            ["workspace", "inspect", ids["ws_emb"]],
            ["advisory", "inspect", ids["advisory"]],
            ["secrets", "inspect", ids["secret"]],
            ["files", "read", ids["main_snapshot"], HIDDEN_PATH],
        ):
            denied = self.outsider(argv)[0]
            self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied", argv)
        # ...and does have something to read, so redaction is exercised.
        public = self.outsider(["files", "list", ids["ws_pub"]])[0]
        self.assertTrue(public["ok"])
        self.assertIn("src/app.txt", {item["path"] for item in public["data"]["files"]})

    def test_cli_sweep_leaks_nothing(self) -> None:
        leaks: list[str] = []
        argvs = sweep_argvs(self.fx)
        swept = 0
        denials = 0
        for argv in argvs:
            for response in ("full", "summary"):
                request_argv = ["--response", response, *argv]
                output, text = self.outsider(request_argv)
                swept += 1
                request_text = " ".join(request_argv)
                leaks += [f"tri {request_text}: {leak}" for leak in self.leaks_in(request_text, text)]
                if response != "full":
                    continue
                # A denial hands the actor a decision to explain. Following it
                # must not name what the denial withheld (a parent workspace, say).
                for diagnostic in output.get("diagnostics", []):
                    decision = diagnostic.get("decision")
                    if diagnostic.get("code") != "policy_denied" or not isinstance(decision, str):
                        continue
                    denials += 1
                    _, explained = self.outsider(["policy", "explain", decision])
                    swept += 1
                    leaks += [f"tri policy explain {decision} (from {request_text}): {leak}" for leak in self.leaks_in(request_text, explained)]
        self.assertGreater(denials, 100, "the sweep should hit many policy denials")
        self.assert_no_leaks(leaks, swept)

    def batch_leaks(self, label: str, commands: list[dict], output: dict, text: str) -> list[str]:
        """Check each batch result against its own argv, then the rest of the
        envelope against the whole plan, so one command's echoed path or id
        cannot excuse another command's response."""
        leaks: list[str] = []
        results = (output.get("data") or {}).get("results") or []
        self.batch_denials += sum(1 for result in results if any(d.get("code") == "policy_denied" for d in result.get("diagnostics", [])))
        for result in results:
            argv = commands[result.get("index", 0)]["argv"]
            leaks += [f"{label} {' '.join(argv)}: {leak}" for leak in self.leaks_in(" ".join(argv), json.dumps(result))]
        rest = {**output, "data": {**(output.get("data") or {}), "results": []}}
        leaks += [f"{label}: {leak}" for leak in self.leaks_in(json.dumps(commands), json.dumps(rest))]
        self.assertEqual(len(results), len(commands), f"{label} should report every command")
        return leaks

    def test_batch_sweep_leaks_nothing(self) -> None:
        argvs = sweep_argvs(self.fx)
        leaks: list[str] = []
        self.batch_denials = 0
        plan = self.root / "plan.json"
        for offset in range(0, len(argvs), 60):
            for response in ("full", "summary"):
                commands = [{"argv": argv} for argv in argvs[offset:offset + 60]]
                plan.write_text(json.dumps({"commands": commands, "continue_on_error": True, "response": response}))
                output, text = self.outsider(["batch", str(plan), "--continue-on-error"])
                leaks += self.batch_leaks(f"batch[{offset}] {response}", commands, output, text)
                # Stdout and stderr outside the JSON envelope must be empty of canaries too.
                leaks += [f"batch[{offset}] stream: {leak}" for leak in self.leaks_in(json.dumps(commands), text[len(json.dumps(output)):])]
        self.assertGreater(self.batch_denials, 100, "batch commands must run as the outsider")
        self.assert_no_leaks(leaks, len(argvs))

    def test_http_sweep_leaks_nothing(self) -> None:
        self.batch_denials = 0
        tokens = self.root / "tokens.json"
        issued, _ = cli.execute_argv(["auth", "issue", OUTSIDER, "--file", str(tokens)])
        self.assertTrue(issued["ok"], issued)
        token = issued["data"]["token"]
        port = self.start_server("--auth-file", str(tokens))
        leaks: list[str] = []
        swept = 0
        for path in self.get_routes():
            status, text = self.call(port, path, token=token)
            swept += 1
            self.assertNotEqual(status, 401, path)
            leaks += [f"GET {path}: {leak}" for leak in self.leaks_in(path, text)]
        denials = 0
        for argv in sweep_argvs(self.fx):
            status, text = self.call(port, "/v0/command", {"argv": argv}, token=token)
            swept += 1
            body = json.loads(text)
            self.assertEqual(body["actor"], OUTSIDER)
            denials += any(d.get("code") == "policy_denied" for d in body.get("diagnostics", []))
            leaks += [f"POST /v0/command {' '.join(argv)}: {leak}" for leak in self.leaks_in(" ".join(argv), text)]
        argvs = sweep_argvs(self.fx)
        for offset in range(0, len(argvs), 80):
            commands = [{"argv": argv} for argv in argvs[offset:offset + 80]]
            status, text = self.call(port, "/v0/batch", {"commands": commands, "continue_on_error": True, "response": "summary"}, token=token)
            swept += 1
            leaks += self.batch_leaks(f"POST /v0/batch[{offset}]", commands, json.loads(text), text)
        self.assertGreater(denials, 100, "the API should hit many policy denials")
        self.assertGreater(self.batch_denials, 100, "API batch commands must run as the outsider")
        self.assert_no_leaks(leaks, swept)

    # HTTP helpers -------------------------------------------------------------

    def get_routes(self) -> list[str]:
        ids = self.fx.ids
        q = lambda value: quote(value, safe="")  # noqa: E731
        routes = ["/v0", "/v0/commands", "/v0/schema", "/v0/projects", "/v0/projects/app", "/v0/artifacts?limit=500", "/v0/events?limit=500", "/v0/conflicts"]
        for sub in ("targets", "packages", "snapshots", "workspaces", "check-sets", "advisories", "releases", "secrets", "exports?limit=500", "events?limit=500"):
            routes.append(f"/v0/projects/app/{sub}")
        routes += ["/v0/projects/app/targets/main", "/v0/projects/app/packages/vault"]
        for word in SEARCH_WORDS:
            routes.append(f"/v0/projects/app/search?q={word}&include_history=true")
            for workspace in ids["workspaces"]:
                routes.append(f"/v0/workspaces/{workspace}/search?q={word}")
        collections = {
            "packages": [ids["vault_package"]],
            "tasks": [ids["task"]],
            "task-runs": [ids["run_priv"]],
            "snapshots": ids["snapshots"],
            "trees": ids["trees"],
            "blobs": ids["blobs"],
            "targets": [ids["target"]],
            "operations": ids["operations"],
            "workspaces": ids["workspaces"],
            "leases": ids["leases"],
            "changesets": ids["changesets"],
            "reviews": ids["reviews"],
            "review-actions": ids["review_actions"],
            "checks": ids["checks"],
            "artifacts": ids["artifacts"],
            "exports": ids["exports"],
            "events": ids["events"],
            "releases": ids["releases"],
            "advisories": [ids["advisory"]],
            "conflicts": ids["conflicts"],
            "grants": ids["grants"],
            "secrets": [ids["secret"]],
        }
        for collection, object_ids in collections.items():
            for object_id in object_ids:
                routes.append(f"/v0/{collection}/{q(object_id)}")
                routes.append(f"/v0/objects/{q(object_id)}")
        for decision in ids["decisions"]:
            routes.append(f"/v0/policy/decisions/{decision}")
        routes.append(f"/v0/tasks/{ids['task']}/attempts")
        for ref in [*ids["workspaces"], *ids["changesets"]]:
            collection = "workspaces" if ref.startswith("ws_") else "changesets"
            routes += [
                f"/v0/{collection}/{ref}/diff?mode=patch",
                f"/v0/{collection}/{ref}/diff?mode=files&path=vault/**",
                f"/v0/{collection}/{ref}/operations",
                f"/v0/{collection}/{ref}/checks",
            ]
        for workspace in ids["workspaces"]:
            routes += [
                f"/v0/workspaces/{workspace}/files",
                f"/v0/workspaces/{workspace}/conflicts",
                f"/v0/workspaces/{workspace}/leases",
                f"/v0/workspaces/{workspace}/events?limit=500",
            ]
            for path in PROBE_PATHS:
                routes.append(f"/v0/workspaces/{workspace}/files/{quote(path, safe='/')}")
        for snapshot in ids["snapshots"]:
            routes.append(f"/v0/snapshots/{snapshot}/files")
            for path in PROBE_PATHS:
                routes.append(f"/v0/snapshots/{snapshot}/files/{quote(path, safe='/')}")
        for check in ids["checks"]:
            routes += [f"/v0/checks/{check}/artifacts", f"/v0/checks/{check}/logs/stdout", f"/v0/checks/{check}/logs/stderr?offset=0&limit=64"]
        for artifact in ids["artifacts"]:
            routes.append(f"/v0/artifacts/{artifact}/content")
        return routes

    def call(self, port: int, path: str, payload: dict | None = None, token: str | None = None) -> tuple[int, str]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with request.urlopen(req, timeout=30) as response:
                return response.status, response.read().decode()
        except HTTPError as exc:
            return exc.code, exc.read().decode()

    @staticmethod
    def stop_server(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
        if os.name == "nt" and os.environ.get("TRI_CONFORMANCE_NATIVE"):
            time.sleep(2)  # tri-server's workers exit on end of input after it is terminated

    def start_server(self, *extra: str) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        # TRI_CONFORMANCE_NATIVE (a tri-server binary) or TRI_CONFORMANCE_SERVER
        # sweeps another server implementation; the default is `tri serve`.
        auth_file = extra[extra.index("--auth-file") + 1] if "--auth-file" in extra else None
        if os.environ.get("TRI_CONFORMANCE_NATIVE") or os.environ.get("TRI_CONFORMANCE_SERVER"):
            argv = server_argv("127.0.0.1", port, auth_file)
        else:
            argv = [sys.executable, "-m", "tri", "serve", "--host", "127.0.0.1", "--port", str(port), "--quiet", *extra]
        proc = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_server, proc)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                status, _ = self.call(port, "/v0/health")
                if status == 200:
                    if os.environ.get("TRI_CONFORMANCE_NATIVE"):
                        _, ready = self.call(port, "/v0/ready")
                        self.assertIn("workers", json.loads(ready)["data"], "expected tri-server to be the server under test")
                    return port
            except (ConnectionError, URLError, TimeoutError):
                time.sleep(0.05)
        self.fail("server did not start")


class LeakSweepCoverageTests(unittest.TestCase):
    """New read routes cannot skip the sweep."""

    def test_every_read_shaped_command_is_swept_or_exempt(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="tri-leak-cover-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.dict(os.environ, {"TRI_HOME": str(root / "tri-home")}):
            fx = LeakSweepFixture(root)
            # Ids only shape argv here; the commands are never run.
            fx.ids = {key: f"{key}_x" for key in ("target", "main_snapshot", "head_priv", "head_emb", "head_pub", "vault_package", "task", "run_priv", "ws_pub", "ws_priv", "ws_emb", "cs_pub", "cs_priv", "check_pub", "check_priv", "advisory", "secret", "base_snapshot")}
            for key in ("workspaces", "changesets", "checks", "reviews", "review_actions", "operations", "snapshots", "trees", "blobs", "releases", "conflicts", "leases", "grants", "artifacts", "exports", "events", "decisions"):
                fx.ids[key] = [f"{key}_x"]
            argvs = sweep_argvs(fx)
        swept = {contract_name_for_argv(argv) for argv in argvs}
        swept.add("batch")  # test_batch_sweep_leaks_nothing and POST /v0/batch
        names = [contract["name"] for contract in cli.COMMAND_CONTRACTS]
        read_shaped = [name for name in names if is_read_shaped(name)]
        self.assertGreater(len(read_shaped), 60)
        missing = [name for name in read_shaped if name not in swept and name not in READ_EXEMPTIONS]
        self.assertEqual(missing, [], "read-shaped commands missing from the leak sweep; sweep them or add a commented exemption")
        stale = [name for name in READ_EXEMPTIONS if name not in names or name in swept]
        self.assertEqual(stale, [], "exemptions that no longer apply")


if __name__ == "__main__":
    unittest.main()
