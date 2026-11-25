"""Tests for changeset descriptions and retroactive `tri changeset describe`."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402


class ChangesetDescribeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-describe-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.name", "Tri Test")
        self.git(self.repo, "config", "user.email", "tri@example.com")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "initial")

        self.home = self.root / "tri-home"
        patcher = mock.patch.dict(os.environ, {"TRI_HOME": str(self.home), "TRI_AUTO_REAP": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_REMOTE", None)
        os.environ.pop("TRI_ACTOR", None)

        self.tri("import", str(self.repo), "--name", "app", "--target", "main")

    def git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout

    def tri(self, *argv: str, ok: bool = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        for diagnostic in output["diagnostics"]:
            self.assertIn(diagnostic["code"], SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        schema = SCHEMA_CATALOG.get(output["schema"])
        if schema and output["ok"]:
            missing = [key for key in schema.get("required", []) if key not in output["data"]]
            self.assertEqual(missing, [], f"{output['schema']} missing {missing}")
        return output

    def target_id(self) -> str:
        return self.tri("targets", "list", "app")["data"]["targets"][0]["id"]

    def target_snapshot(self) -> str:
        return self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]

    def make_checked_changeset(
        self,
        path: str,
        text: str,
        *,
        title: str,
        description: str | None = None,
        description_file: str | None = None,
        actor: str = "agent:a",
        ws_visibility: str = "public",
        cs_visibility: str | None = None,
    ) -> tuple[str, str]:
        ws = self.tri("--actor", actor, "workspace", "create", "app", "--from", "main", "--visibility", ws_visibility)["data"]["id"]
        self.tri("--actor", actor, "files", "write", ws, path, "--text", text)
        self.tri("--actor", actor, "run", ws, "--", sys.executable, "-c", "pass")
        argv = ["--actor", actor, "changeset", "create", ws, "--title", title]
        if description is not None:
            argv += ["--description", description]
        if description_file is not None:
            argv += ["--description-file", description_file]
        if cs_visibility is not None:
            argv += ["--visibility", cs_visibility]
        cs = self.tri(*argv)["data"]["id"]
        return ws, cs

    def approve_changeset(self, cs_id: str) -> str:
        review = self.tri("review", "create", cs_id)["data"]["id"]
        self.tri("review", "approve", review)
        return review

    def test_create_split_and_retroactive_describe_after_merge_without_rewriting_history(self) -> None:
        desc_file = self.root / "initial-desc.md"
        desc_file.write_text("Initial draft description.\n", encoding="utf-8")
        # Create both workspaces from the same initial target so the first merges
        # fast-forward and the second merges via 3-way line merge.
        ws_first = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]["id"]
        ws_second = self.tri("--actor", "agent:b", "workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]["id"]

        self.tri("--actor", "agent:a", "files", "write", ws_first, "src/app.py", "--text", "LINE1\nline2\nline3\n")
        self.tri("--actor", "agent:a", "run", ws_first, "--", sys.executable, "-c", "pass")
        first = self.tri("--actor", "agent:a", "changeset", "create", ws_first, "--title", "WIP fix", "--description-file", str(desc_file))["data"]["id"]
        review_first = self.approve_changeset(first)
        self.tri("changeset", "merge", first, "--target", "main")
        first_merged_snap = self.target_snapshot()

        # Second change produces a 3-way merge snapshot on top of the first.
        self.tri("--actor", "agent:b", "files", "write", ws_second, "src/app.py", "--text", "line1\nline2\nLINE3\n")
        self.tri("--actor", "agent:b", "run", ws_second, "--", sys.executable, "-c", "pass")
        second = self.tri("--actor", "agent:b", "changeset", "create", ws_second, "--title", "Draft second edit", "--description", "Second draft notes")["data"]["id"]
        self.approve_changeset(second)
        self.tri("changeset", "merge", second, "--target", "main")
        second_merged_snap = self.target_snapshot()

        first_before = self.tri("changeset", "inspect", first)["data"]
        second_before = self.tri("changeset", "inspect", second)["data"]
        first_snap_before = self.tri("snapshots", "inspect", first_merged_snap)["data"]
        second_snap_before = self.tri("snapshots", "inspect", second_merged_snap)["data"]
        self.assertEqual(first_before["description"], "Initial draft description.\n")

        # Retroactively update both merged changesets without touching history.
        updated_first = self.tri(
            "--actor",
            "agent:a",
            "changeset",
            "describe",
            first,
            "--title",
            "Normalize first line header",
            "--description",
            "Clarifies line 1 casing after production review.",
            "--reason",
            "clarify-post-merge",
        )
        self.assertEqual(updated_first["schema"], "tri.changeset.describe")
        self.assertEqual(len(updated_first["events"]), 1)
        data1 = updated_first["data"]
        self.assertFalse(data1["history_rewritten"])
        self.assertTrue(data1["updated"])
        self.assertEqual(data1["changed_fields"], ["title", "description"])
        self.assertEqual(data1["previous_title"], "WIP fix")
        self.assertEqual(data1["previous_description"], "Initial draft description.\n")
        self.assertEqual(data1["title"], "Normalize first line header")
        self.assertEqual(data1["description"], "Clarifies line 1 casing after production review.")
        self.assertEqual(data1["status"], "merged")
        self.assertEqual(data1["base_snapshot"], first_before["base_snapshot"])
        self.assertEqual(data1["head_snapshot"], first_before["head_snapshot"])
        self.assertEqual(data1["lifecycle"]["description_revisions"], 1)
        self.assertEqual(data1["lifecycle"]["described_by"], "agent:a")
        self.assertEqual(data1["lifecycle"]["last_describe_reason"], "clarify-post-merge")
        self.assertEqual(data1["lifecycle"]["last_describe_event"], updated_first["events"][0])

        updated_second = self.tri(
            "--actor",
            "agent:b",
            "changeset",
            "describe",
            second,
            "--title",
            "Uppercase footer line",
            "--description",
            "Updates line 3 without conflicting with line 1.",
        )
        self.assertEqual(updated_second["data"]["changed_fields"], ["title", "description"])

        # Snapshot graph, fingerprints, parents, and target tip are completely unchanged.
        self.assertEqual(self.target_snapshot(), second_merged_snap)
        first_snap_after = self.tri("snapshots", "inspect", first_merged_snap)["data"]
        second_snap_after = self.tri("snapshots", "inspect", second_merged_snap)["data"]
        for key in ("id", "root_tree", "parents", "content_fingerprint"):
            self.assertEqual(first_snap_after[key], first_snap_before[key])
            self.assertEqual(second_snap_after[key], second_snap_before[key])
        self.assertEqual(self.tri("changeset", "inspect", second)["data"]["head_snapshot"], second_before["head_snapshot"])

        # History queries (files log & blame), review inspect, compare, and search all show the updated text.
        target = self.target_id()
        log_entries = self.tri("files", "log", target, "src/app.py")["data"]["entries"]
        by_cs = {entry["changeset"]: entry for entry in log_entries if entry.get("changeset")}
        self.assertEqual(by_cs[first]["changeset_title"], "Normalize first line header")
        self.assertEqual(by_cs[first]["changeset_description"], "Clarifies line 1 casing after production review.")
        self.assertEqual(by_cs[second]["changeset_title"], "Uppercase footer line")
        self.assertEqual(by_cs[second]["changeset_description"], "Updates line 3 without conflicting with line 1.")

        blame = self.tri("files", "blame", target, "src/app.py")["data"]
        by_line = {line: item for item in blame["ranges"] for line in range(item["start"], item["end"] + 1)}
        self.assertEqual(by_line[1]["changeset"], first)
        self.assertEqual(by_line[1]["changeset_title"], "Normalize first line header")
        self.assertEqual(by_line[1]["changeset_description"], "Clarifies line 1 casing after production review.")
        self.assertEqual(by_line[3]["changeset"], second)
        self.assertEqual(by_line[3]["changeset_title"], "Uppercase footer line")

        review_view = self.tri("review", "inspect", review_first)["data"]
        self.assertEqual(review_view["changeset_summary"]["title"], "Normalize first line header")
        self.assertEqual(review_view["changeset_summary"]["description"], "Clarifies line 1 casing after production review.")

        compared = self.tri("changeset", "compare", first, second)["data"]["changesets"]
        self.assertEqual(compared[0]["description"], "Clarifies line 1 casing after production review.")
        self.assertEqual(compared[1]["description"], "Updates line 3 without conflicting with line 1.")

        search = self.tri("search", "app", "production review", "--scope", "changesets")["data"]
        self.assertTrue(any(m["object"] == first and m["field"] == "description" for m in search["matches"]), search)

        # Append-only audit event preserves previous and new text.
        evt = self.tri("events", "inspect", updated_first["events"][0])["data"]
        self.assertEqual(evt["kind"], "changeset.described")
        self.assertEqual(evt["object_refs"]["previous_title"], "WIP fix")
        self.assertEqual(evt["object_refs"]["title"], "Normalize first line header")
        self.assertEqual(evt["object_refs"]["previous_description"], "Initial draft description.\n")
        self.assertEqual(evt["object_refs"]["description"], "Clarifies line 1 casing after production review.")
        self.assertEqual(evt["object_refs"]["reason"], "clarify-post-merge")

        # Idempotent no-op emits no new event and leaves revision counter unchanged.
        noop = self.tri(
            "--actor",
            "agent:a",
            "changeset",
            "describe",
            first,
            "--title",
            "Normalize first line header",
        )
        self.assertEqual(noop["events"], [])
        self.assertFalse(noop["data"]["updated"])
        self.assertEqual(noop["data"]["changed_fields"], [])
        self.assertEqual(noop["data"]["lifecycle"]["description_revisions"], 1)

        # Clearing description with empty string works cleanly.
        cleared = self.tri("--actor", "agent:a", "changeset", "describe", first, "--description", "")
        self.assertEqual(cleared["data"]["description"], "")
        self.assertEqual(cleared["data"]["changed_fields"], ["description"])
        self.assertEqual(cleared["data"]["lifecycle"]["description_revisions"], 2)

    def test_changeset_split_accepts_description(self) -> None:
        ws = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws, "src/one.py", "--text", "1\n")
        self.tri("--actor", "agent:a", "files", "write", ws, "src/two.py", "--text", "2\n")
        source = self.tri("--actor", "agent:a", "changeset", "create", ws, "--title", "Combined")["data"]["id"]

        split = self.tri(
            "--actor",
            "agent:a",
            "changeset",
            "split",
            source,
            "--paths",
            "src/one.py",
            "--apply",
            "--title",
            "Part one",
            "--description",
            "Only the first file.",
        )["data"]
        self.assertEqual(split["created"]["title"], "Part one")
        self.assertEqual(split["created"]["description"], "Only the first file.")

    def test_describe_validation_errors(self) -> None:
        _, cs = self.make_checked_changeset("src/app.py", "changed\n", title="Initial")
        missing_fields = self.tri("--actor", "agent:a", "changeset", "describe", cs, ok=False)
        self.assertEqual(missing_fields["diagnostics"][0]["code"], "invalid_command")

        empty_title = self.tri("--actor", "agent:a", "changeset", "describe", cs, "--title", "   ", ok=False)
        self.assertEqual(empty_title["diagnostics"][0]["code"], "invalid_command")

        desc_file = self.root / "desc.txt"
        desc_file.write_text("from file", encoding="utf-8")
        both = self.tri("--actor", "agent:a", "changeset", "describe", cs, "--description", "inline", "--description-file", str(desc_file), ok=False)
        self.assertEqual(both["diagnostics"][0]["code"], "invalid_command")

        missing_file = self.tri("--actor", "agent:a", "changeset", "describe", cs, "--description-file", str(self.root / "nope.txt"), ok=False)
        self.assertEqual(missing_file["diagnostics"][0]["code"], "path_not_found")

    def test_describe_policy_and_private_changeset_redaction(self) -> None:
        # Private changeset inside a public workspace must not leak description edits.
        _, cs = self.make_checked_changeset(
            "src/app.py",
            "secret fix\n",
            title="SECRET_TITLE_CANARY",
            description="SECRET_DESC_CANARY",
            actor="agent:a",
            ws_visibility="public",
            cs_visibility="private",
        )
        # Unauthorized actor cannot describe it.
        denied = self.tri("--actor", "agent:b", "changeset", "describe", cs, "--title", "hijack", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")

        # Owner updates description; audit event must stay hidden from agent:b despite public workspace.
        described = self.tri(
            "--actor",
            "agent:a",
            "changeset",
            "describe",
            cs,
            "--title",
            "UPDATED_PRIVATE_TITLE_CANARY",
            "--description",
            "UPDATED_PRIVATE_DESC_CANARY",
        )
        evt_id = described["events"][0]
        self.assertEqual(self.tri("--actor", "agent:b", "events", "inspect", evt_id, ok=False)["diagnostics"][0]["code"], "policy_denied")
        events_for_b = json.dumps(self.tri("--actor", "agent:b", "events", "list", "--project", "app"))
        self.assertNotIn("UPDATED_PRIVATE_TITLE_CANARY", events_for_b)
        self.assertNotIn("UPDATED_PRIVATE_DESC_CANARY", events_for_b)
        self.assertNotIn("SECRET_DESC_CANARY", events_for_b)

        search_for_b = self.tri("--actor", "agent:b", "search", "app", "UPDATED_PRIVATE_DESC_CANARY", "--scope", "changesets")["data"]
        self.assertEqual(search_for_b["matches"], [])
        self.assertEqual(search_for_b["policy_denied_skipped"], 1)

        # Granting changeset.write lets agent:b describe it.
        self.tri("policy", "grant", cs, "--principal", "actor:agent:b", "--action", "changeset.write")
        granted = self.tri("--actor", "agent:b", "changeset", "describe", cs, "--description", "Updated by grantee")
        self.assertEqual(granted["data"]["description"], "Updated by grantee")

    def test_export_git_commit_and_github_pr_use_updated_description(self) -> None:
        _, cs = self.make_checked_changeset("src/app.py", "line1\nupdated\nline3\n", title="Old title", description="Old body")
        self.approve_changeset(cs)
        self.tri(
            "--actor",
            "agent:a",
            "changeset",
            "describe",
            cs,
            "--title",
            "Final release title",
            "--description",
            "Detailed rationale for reviewers and git log.",
        )

        # Export as GitHub PR first (leaves repo on new branch, so use a separate clean clone for git-commit).
        pr_repo = self.root / "pr-repo"
        self.git(self.root, "clone", "-q", str(self.repo), str(pr_repo))
        self.git(pr_repo, "config", "user.name", "Tri Test")
        self.git(pr_repo, "config", "user.email", "tri@example.com")
        exported_pr = self.tri("export", "github-pr", cs, "--repo", str(pr_repo), "--branch", "feat/describe")["data"]
        pr_artifact = json.loads(self.tri("artifacts", "read", exported_pr["artifact"])["data"]["text"])
        self.assertEqual(pr_artifact["title"], "Final release title")
        self.assertIn("Detailed rationale for reviewers and git log.", pr_artifact["body"])
        pr_commit_msg = self.git(pr_repo, "log", "-1", "--format=%B", exported_pr["commit"])
        self.assertIn("Final release title", pr_commit_msg)
        self.assertIn("Detailed rationale for reviewers and git log.", pr_commit_msg)

        # Export as Git commit uses title + description by default.
        commit_repo = self.root / "commit-repo"
        self.git(self.root, "clone", "-q", str(self.repo), str(commit_repo))
        self.git(commit_repo, "config", "user.name", "Tri Test")
        self.git(commit_repo, "config", "user.email", "tri@example.com")
        exported_commit = self.tri("export", "git-commit", cs, "--repo", str(commit_repo))["data"]
        commit_msg = self.git(commit_repo, "log", "-1", "--format=%B", exported_commit["commit"])
        self.assertIn("Final release title", commit_msg)
        self.assertIn("Detailed rationale for reviewers and git log.", commit_msg)

    def test_existing_store_migrates_changesets_description_column(self) -> None:
        _, cs = self.make_checked_changeset("src/app.py", "v2\n", title="Before migration")
        # Simulate a legacy SQLite database without the `description` column.
        store = cli.Store(self.home)
        try:
            cols = [row["name"] for row in store.conn.execute("PRAGMA table_info(changesets)").fetchall()]
            self.assertIn("description", cols)
            keep_cols = [col for col in cols if col != "description"]
            col_list = ", ".join(keep_cols)
            with store.conn:
                store.conn.execute(f"CREATE TABLE changesets_legacy AS SELECT {col_list} FROM changesets")
                store.conn.execute("DROP TABLE changesets")
                store.conn.execute("ALTER TABLE changesets_legacy RENAME TO changesets")
                store.conn.execute("DELETE FROM store_meta WHERE key = 'schema_fingerprint'")
        finally:
            store.close()
        cli._SQLITE_SCHEMA_READY.clear()

        inspected = self.tri("changeset", "inspect", cs)["data"]
        self.assertEqual(inspected["description"], "")
        described = self.tri("--actor", "agent:a", "changeset", "describe", cs, "--description", "Added after migration")["data"]
        self.assertEqual(described["description"], "Added after migration")


if __name__ == "__main__":
    unittest.main()
