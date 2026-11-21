"""Two-way Git mirror: Tri targets and Git branches stay in sync without leaking hidden paths."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli, gitmirror  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402

GUIDE = "a\nb\nc\nd\ne\nf\ng\n"


class GitMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-mirror-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q", "-b", "main")
        (self.source / "docs").mkdir()
        (self.source / "vendor" / "secret_sdk").mkdir(parents=True)
        (self.source / "docs" / "guide.md").write_text(GUIDE)
        (self.source / "src.txt").write_text("one\n")
        (self.source / "vendor" / "secret_sdk" / "core.txt").write_text("private v1\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-q", "-m", "initial")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.source), str(self.remote)], check=True)
        self.clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.clone)], check=True)
        self.git(self.clone, "config", "user.email", "pat@example.com")
        self.git(self.clone, "config", "user.name", "Pat Human")
        env = {"TRI_HOME": str(self.root / "tri-home")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        self.tri("import", str(self.source), "--name", "app")
        self.tri("merge-rules", "add", "app", "all", "--path", "*")

    # helpers ---------------------------------------------------------------

    def git(self, repo: Path, *args: str) -> str:
        env = dict(os.environ)
        if repo == self.source:
            env.update({"GIT_AUTHOR_NAME": "Tri Test", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "Tri Test", "GIT_COMMITTER_EMAIL": "t@example.com"})
        return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, text=True, env=env).stdout

    def tri(self, *argv: str, ok: bool | None = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        if ok is True:
            self.assertTrue(output["ok"], output)
        elif ok is False:
            self.assertFalse(output["ok"], output)
        advertised = set(SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        for diagnostic in output["diagnostics"] + output.get("warnings", []):
            self.assertIn(diagnostic["code"], advertised)
        schema = SCHEMA_CATALOG.get(output["schema"])
        if schema and output["ok"]:
            missing = [key for key in schema.get("required", []) if key not in output["data"]]
            self.assertEqual(missing, [], f"{output['schema']} missing {missing}")
        return output

    def tri_merge(self, path: str, text: str, *, actor: str = "agent:a") -> str:
        workspace = self.tri("--actor", actor, "workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("--actor", actor, "files", "write", workspace, path, "--text", text)
        self.tri("--actor", actor, "run", workspace, "--", sys.executable, "-c", "pass")
        changeset = self.tri("--actor", actor, "changeset", "create", workspace, "--title", f"Edit {path}")["data"]["id"]
        self.tri("--actor", actor, "changeset", "merge", changeset, "--target", "main")
        return changeset

    def git_push(self, path: str, text: str, message: str = "git edit") -> str:
        self.git(self.clone, "pull", "-q", "--rebase")
        target = self.clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git(self.clone, "add", "-A")
        self.git(self.clone, "commit", "-q", "-m", message)
        self.git(self.clone, "push", "-q")
        return self.git(self.clone, "rev-parse", "HEAD").strip()

    def remote_file(self, path: str, ref: str = "main") -> str | None:
        result = subprocess.run(["git", f"--git-dir={self.remote}", "show", f"{ref}:{path}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout if result.returncode == 0 else None

    def target_file(self, path: str) -> str:
        snapshot = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        return self.tri("files", "read", snapshot, path)["data"]["text"]

    def add_mirror(self, *extra: str, ok: bool | None = True) -> dict:
        return self.tri("mirror", "add", "app", "--remote", str(self.remote), *extra, ok=ok)

    def sync(self, ok: bool | None = True) -> dict:
        return self.tri("mirror", "sync", "app", ok=ok)

    # tests -----------------------------------------------------------------

    def test_add_pairs_identical_trees_without_new_commits(self) -> None:
        mirror = self.add_mirror()["data"]
        self.assertEqual(mirror["last_result"]["mode"], "initial_pair")
        self.assertTrue(mirror["in_sync"])
        self.assertEqual(mirror["synced_git_commit"], self.git(self.source, "rev-parse", "HEAD").strip())
        self.assertEqual(self.sync()["data"]["sync"]["mode"], "noop")

    def test_tri_merges_become_git_commits_with_changeset_titles(self) -> None:
        self.add_mirror()
        first = self.tri_merge("src.txt", "two\n")
        self.tri_merge("docs/new.md", "hello\n", actor="agent:b")
        result = self.sync()["data"]
        self.assertEqual(result["sync"]["mode"], "outbound")
        self.assertEqual(len(result["sync"]["outbound_commits"]), 2)
        self.assertEqual(self.remote_file("src.txt"), "two\n")
        self.assertEqual(self.remote_file("docs/new.md"), "hello\n")
        log = subprocess.run(["git", f"--git-dir={self.remote}", "log", "--format=%an|%s|%b", "main"], stdout=subprocess.PIPE, text=True, check=True).stdout
        self.assertIn("agent:a|Edit src.txt|", log)
        self.assertIn(f"Tri-Changeset: {first}", log)
        self.assertIn("agent:b|Edit docs/new.md|", log)
        self.assertTrue(result["in_sync"])

    def test_git_pushes_land_as_attributed_tri_snapshots(self) -> None:
        self.add_mirror()
        commit = self.git_push("src.txt", "from git\n")
        result = self.sync()["data"]
        self.assertEqual(result["sync"]["mode"], "inbound")
        self.assertEqual(result["sync"]["inbound_commits"], [commit])
        self.assertEqual(self.target_file("src.txt"), "from git\n")
        target = self.tri("targets", "list", "app")["data"]["targets"][0]["id"]
        ranges = self.tri("files", "blame", target, "src.txt")["data"]["ranges"]
        self.assertEqual(ranges[0]["created_by"], "human:pat_example_com")
        self.assertEqual(ranges[0]["git_commit"], commit)

    def test_concurrent_edits_on_both_sides_merge_and_push_a_merge_commit(self) -> None:
        self.add_mirror()
        self.tri_merge("docs/guide.md", "A\nb\nc\nd\ne\nf\ng\n")
        self.git_push("docs/guide.md", "a\nb\nc\nd\ne\nf\nG\n")
        result = self.sync()["data"]
        self.assertEqual(result["sync"]["mode"], "merge")
        self.assertEqual(result["sync"]["text_merged_paths"], ["docs/guide.md"])
        self.assertEqual(self.target_file("docs/guide.md"), "A\nb\nc\nd\ne\nf\nG\n")
        self.assertEqual(self.remote_file("docs/guide.md"), "A\nb\nc\nd\ne\nf\nG\n")
        parents = subprocess.run(["git", f"--git-dir={self.remote}", "log", "-1", "--format=%P", "main"], stdout=subprocess.PIPE, text=True, check=True).stdout.split()
        self.assertEqual(len(parents), 2)
        self.assertTrue(result["in_sync"])
        self.git(self.clone, "pull", "-q", "--rebase")
        self.assertEqual((self.clone / "docs" / "guide.md").read_text(), "A\nb\nc\nd\ne\nf\nG\n")

    def test_same_line_edits_report_a_mirror_conflict_and_change_nothing(self) -> None:
        self.add_mirror()
        self.tri_merge("src.txt", "tri\n")
        before = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        git_tip = self.git_push("src.txt", "git\n")
        result = self.sync(ok=False)
        self.assertEqual(result["diagnostics"][0]["code"], "mirror_conflict")
        self.assertEqual(result["diagnostics"][0]["conflicts"][0]["path"], "src.txt")
        self.assertEqual(self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"], before)
        self.assertEqual(subprocess.run(["git", f"--git-dir={self.remote}", "rev-parse", "main"], stdout=subprocess.PIPE, text=True).stdout.strip(), git_tip)

    def test_hidden_package_never_reaches_git_and_git_cannot_overwrite_it(self) -> None:
        self.tri("packages", "declare", "app", "secret_sdk", "--path", "vendor/secret_sdk", "--visibility", "private")
        self.tri("mirror", "add", "app", "--remote", str(self.remote), "--prefer", "tri")
        self.tri_merge("vendor/secret_sdk/core.txt", "private v2 CANARY\n", actor="actor_local")
        self.tri_merge("src.txt", "public change\n")
        self.sync()
        self.assertNotEqual(self.remote_file("vendor/secret_sdk/core.txt"), "private v2 CANARY\n")
        all_objects = subprocess.run(["git", f"--git-dir={self.remote}", "log", "--all", "-p"], stdout=subprocess.PIPE, text=True, check=True).stdout
        self.assertNotIn("CANARY", all_objects)
        self.assertEqual(self.remote_file("src.txt"), "public change\n")
        self.git_push("vendor/secret_sdk/core.txt", "overwritten from git\n")
        result = self.sync()["data"]
        self.assertIn("vendor/secret_sdk/core.txt", result["sync"]["ignored_git_paths"])
        self.assertEqual(self.target_file("vendor/secret_sdk/core.txt"), "private v2 CANARY\n")

    def test_secret_like_files_are_never_mirrored_either_way(self) -> None:
        self.add_mirror()
        self.git_push(".env", "TOKEN=from-git\n")
        result = self.sync()["data"]
        self.assertIn(".env", result["sync"]["ignored_git_paths"])
        snapshot = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        listed = [item["path"] for item in self.tri("files", "list", snapshot)["data"]["files"] if item.get("path")]
        self.assertNotIn(".env", listed)

    def test_empty_remote_gets_an_initial_push(self) -> None:
        empty = self.root / "empty.git"
        subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True)
        mirror = self.tri("mirror", "add", "app", "--remote", str(empty))["data"]
        self.assertEqual(mirror["last_result"]["mode"], "initial_push")
        shown = subprocess.run(["git", f"--git-dir={empty}", "show", "main:docs/guide.md"], stdout=subprocess.PIPE, text=True, check=True).stdout
        self.assertEqual(shown, GUIDE)

    def test_divergent_start_requires_a_preference(self) -> None:
        self.git_push("src.txt", "diverged\n")
        added = self.add_mirror(ok=False)
        self.assertEqual(added["diagnostics"][0]["code"], "mirror_diverged")
        self.tri("mirror", "remove", "app")
        adopted = self.add_mirror("--prefer", "git")["data"]
        self.assertEqual(adopted["last_result"]["mode"], "initial_adopt_git")
        self.assertEqual(self.target_file("src.txt"), "diverged\n")

    def test_push_race_retries_after_refetch(self) -> None:
        self.add_mirror()
        self.tri_merge("src.txt", "two\n")
        real_push = gitmirror.push
        calls = {"count": 0}

        def racing_push(git_dir, remote, sha, branch):
            calls["count"] += 1
            if calls["count"] == 1:
                self.git_push("docs/other.md", "raced in\n")
            return real_push(git_dir, remote, sha, branch)

        with mock.patch.object(gitmirror, "push", racing_push):
            result = self.sync()["data"]
        self.assertGreaterEqual(result["sync"]["attempts"], 2)
        self.assertEqual(self.remote_file("src.txt"), "two\n")
        self.assertEqual(self.remote_file("docs/other.md"), "raced in\n")
        self.assertEqual(self.target_file("docs/other.md"), "raced in\n")

    def test_only_maintainers_manage_mirrors(self) -> None:
        denied = self.tri("--actor", "agent:a", "mirror", "add", "app", "--remote", str(self.remote), ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        self.add_mirror()
        self.assertEqual(self.tri("--actor", "agent:a", "mirror", "sync", "app", ok=False)["diagnostics"][0]["code"], "policy_denied")
        listed = self.tri("mirror", "list")["data"]["mirrors"]
        self.assertEqual(len(listed), 1)

    def test_watch_runs_bounded_cycles(self) -> None:
        self.add_mirror()
        self.git_push("src.txt", "watched\n")
        result = self.tri("mirror", "sync", "app", "--watch", "--interval", "0.05", "--max-cycles", "2")["data"]
        self.assertEqual(result["cycles"], 2)
        self.assertEqual(self.target_file("src.txt"), "watched\n")


if __name__ == "__main__":
    unittest.main()
