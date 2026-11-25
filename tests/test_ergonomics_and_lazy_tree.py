"""Tests for lazy per-directory listing (`tri files ls`) and task/changeset/review list commands."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib import request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402


class ErgonomicsAndLazyTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-ergo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src" / "core").mkdir(parents=True)
        (repo / "docs").mkdir(parents=True)
        (repo / "vault").mkdir(parents=True)
        (repo / "env_only").mkdir(parents=True)
        (repo / "README.md").write_text("root readme\n", encoding="utf-8")
        (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
        (repo / "src" / "core" / "engine.py").write_text("engine = 1\n", encoding="utf-8")
        (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
        (repo / "vault" / "internal.txt").write_text("VAULT_SECRET_TEXT\n", encoding="utf-8")
        (repo / "env_only" / ".env").write_text("API_KEY=secret\n", encoding="utf-8")

        self.home = self.root / "tri-home"
        patcher = mock.patch.dict(os.environ, {"TRI_HOME": str(self.home), "TRI_AUTO_REAP": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_REMOTE", None)
        os.environ.pop("TRI_ACTOR", None)

        self.tri("import", str(repo), "--name", "app", "--target", "main")
        self.tri("packages", "declare", "app", "vault", "--path", "vault/**", "--visibility", "private")

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

    def target_snapshot(self) -> str:
        return self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]

    def test_files_ls_walks_only_requested_directory_and_enforces_policy(self) -> None:
        ws = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]["id"]
        # Root listing omits hidden package `vault/` and secret-only `secret_only/` for non-maintainers.
        root_view = self.tri("--actor", "agent:a", "files", "ls", ws)["data"]
        self.assertEqual(root_view["directory"], "")
        self.assertEqual([d["name"] for d in root_view["directories"]], ["docs", "src"])
        self.assertEqual([f["name"] for f in root_view["files"]], ["README.md"])
        self.assertGreaterEqual(root_view["redacted_skipped"], 2)
        self.assertGreaterEqual(root_view["policy_denied_skipped"], 1)
        self.assertNotIn("vault", json.dumps(root_view))
        self.assertNotIn("env_only", json.dumps(root_view))

        # Listing a nested directory reads only nodes along its path, never flattening unrelated trees.
        store = cli.Store(self.home)
        self.addCleanup(store.close)
        read_trees: list[str] = []
        orig_row_text = store._tree_row_text

        def spy_tree_row(tree_id: str) -> str:
            read_trees.append(tree_id)
            return orig_row_text(tree_id)

        with mock.patch.object(store, "_tree_row_text", side_effect=spy_tree_row):
            output, _ = cli.execute_argv(["--actor", "agent:a", "files", "ls", ws, "src/core"], store=store)
        self.assertTrue(output["ok"], output)
        self.assertEqual(output["data"]["directory"], "src/core")
        self.assertEqual([f["path"] for f in output["data"]["files"]], ["src/core/engine.py"])
        self.assertEqual(output["data"]["directories"], [])
        # Root -> src -> core = 3 tree node reads (docs/, vault/, env_only/ untouched!).
        self.assertEqual(len(read_trees), 3, read_trees)

        # Hidden package directory is refused with policy_denied; secret-only directory appears nonexistent.
        denied_vault = self.tri("--actor", "agent:a", "files", "ls", ws, "vault", ok=False)
        self.assertEqual(denied_vault["diagnostics"][0]["code"], "policy_denied")
        hidden_secret_dir = self.tri("--actor", "agent:a", "files", "ls", ws, "env_only", ok=False)
        self.assertEqual(hidden_secret_dir["diagnostics"][0]["code"], "path_not_found")

        # File path and nonexistent directory return structured errors.
        file_err = self.tri("--actor", "agent:a", "files", "ls", ws, "src/app.py", ok=False)
        self.assertEqual(file_err["diagnostics"][0]["code"], "invalid_path")
        missing_err = self.tri("--actor", "agent:a", "files", "ls", ws, "does/not/exist", ok=False)
        self.assertEqual(missing_err["diagnostics"][0]["code"], "path_not_found")

        # Maintainer sees vault/ (but secret_only/.env is still secret-like).
        maintainer_root = self.tri("files", "ls", ws)["data"]
        self.assertEqual([d["name"] for d in maintainer_root["directories"]], ["docs", "src", "vault"])
        maintainer_vault = self.tri("files", "ls", ws, "vault")["data"]
        self.assertEqual([f["path"] for f in maintainer_vault["files"]], ["vault/internal.txt"])

    def test_task_changeset_and_review_list_commands_filter_and_redact(self) -> None:
        task_a = self.tri("--actor", "agent:a", "task", "create", "app", "--title", "Public task A")["data"]["id"]
        task_b = self.tri("--actor", "agent:a", "task", "create", "app", "--title", "Public task B")["data"]["id"]
        self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--task", task_b)
        self.tri("task", "block", task_b, "--reason", "waiting")

        ws_pub = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--task", task_a, "--visibility", "public")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws_pub, "src/app.py", "--text", "print('pub')\n")
        cs_pub = self.tri("--actor", "agent:a", "changeset", "create", ws_pub, "--title", "Public change", "--description", "Visible notes")["data"]["id"]
        rev_pub = self.tri("--actor", "agent:a", "review", "create", cs_pub)["data"]["id"]

        ws_priv = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "private")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws_priv, "src/app.py", "--text", "print('priv')\n")
        cs_priv = self.tri("--actor", "agent:a", "changeset", "create", ws_priv, "--title", "PRIVATE_CS_TITLE", "--visibility", "private")["data"]["id"]
        rev_priv = self.tri("--actor", "agent:a", "review", "create", cs_priv)["data"]["id"]

        # Maintainer sees everything and can filter by status, workspace, changeset, limit.
        all_tasks = self.tri("task", "list", "app")["data"]
        self.assertEqual({t["id"] for t in all_tasks["tasks"]}, {task_a, task_b})
        self.assertEqual(all_tasks["redacted"], 0)
        blocked_tasks = self.tri("task", "list", "app", "--status", "blocked")["data"]
        self.assertEqual([t["id"] for t in blocked_tasks["tasks"]], [task_b])
        limited_tasks = self.tri("task", "list", "app", "--limit", "1")["data"]
        self.assertEqual(len(limited_tasks["tasks"]), 1)

        all_cs = self.tri("changeset", "list", "app")["data"]
        self.assertEqual({c["id"] for c in all_cs["changesets"]}, {cs_pub, cs_priv})
        ws_filtered_cs = self.tri("changeset", "list", "app", "--workspace", ws_pub)["data"]
        self.assertEqual([c["id"] for c in ws_filtered_cs["changesets"]], [cs_pub])
        self.assertEqual(ws_filtered_cs["changesets"][0]["description"], "Visible notes")

        all_reviews = self.tri("review", "list", "app")["data"]
        self.assertEqual({r["id"] for r in all_reviews["reviews"]}, {rev_pub, rev_priv})
        cs_filtered_rev = self.tri("review", "list", "app", "--changeset", cs_pub)["data"]
        self.assertEqual([r["id"] for r in cs_filtered_rev["reviews"]], [rev_pub])

        # Unprivileged actor sees only public changeset & review; private ones are redacted without leaking ids/titles.
        cs_for_b = self.tri("--actor", "agent:b", "changeset", "list", "app")["data"]
        self.assertEqual([c["id"] for c in cs_for_b["changesets"]], [cs_pub])
        self.assertEqual(cs_for_b["redacted"], 1)
        self.assertNotIn(cs_priv, json.dumps(cs_for_b))
        self.assertNotIn("PRIVATE_CS_TITLE", json.dumps(cs_for_b))

        rev_for_b = self.tri("--actor", "agent:b", "review", "list", "app")["data"]
        self.assertEqual([r["id"] for r in rev_for_b["reviews"]], [rev_pub])
        self.assertEqual(rev_for_b["redacted"], 1)
        self.assertNotIn(rev_priv, json.dumps(rev_for_b))

    def test_http_get_routes_for_lists_and_tree(self) -> None:
        ws = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws, "docs/guide.md", "--text", "v2\n")
        cs = self.tri("--actor", "agent:a", "changeset", "create", ws, "--title", "HTTP list test")["data"]["id"]
        rev = self.tri("--actor", "agent:a", "review", "create", cs)["data"]["id"]
        task = self.tri("--actor", "agent:a", "task", "create", "app", "--title", "HTTP task")["data"]["id"]
        snap = self.target_snapshot()

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "TRI_HOME": str(self.home), "TRI_AUTO_REAP": "0"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "tri", "serve", "--host", "127.0.0.1", "--port", str(port), "--quiet"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def stop() -> None:
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)

        self.addCleanup(stop)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with request.urlopen(f"http://127.0.0.1:{port}/v0/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except (URLError, ConnectionError, TimeoutError):
                time.sleep(0.05)

        def get_json(path: str) -> dict:
            with request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
                return json.loads(resp.read())

        tasks = get_json("/v0/projects/app/tasks")
        self.assertTrue(tasks["ok"])
        self.assertEqual([t["id"] for t in tasks["data"]["tasks"]], [task])

        changesets = get_json(f"/v0/changesets?project=app&workspace={ws}")
        self.assertTrue(changesets["ok"])
        self.assertEqual([c["id"] for c in changesets["data"]["changesets"]], [cs])

        reviews = get_json(f"/v0/projects/app/reviews?changeset={cs}")
        self.assertTrue(reviews["ok"])
        self.assertEqual([r["id"] for r in reviews["data"]["reviews"]], [rev])

        tree = get_json(f"/v0/workspaces/{ws}/tree?dir=src")
        self.assertTrue(tree["ok"])
        self.assertEqual([d["name"] for d in tree["data"]["directories"]], ["core"])
        self.assertEqual([f["name"] for f in tree["data"]["files"]], ["app.py"])

        snap_tree = get_json(f"/v0/snapshots/{snap}/tree?dir=docs")
        self.assertTrue(snap_tree["ok"])
        self.assertEqual([f["path"] for f in snap_tree["data"]["files"]], ["docs/guide.md"])

    def test_changeset_refresh_updates_open_changeset_and_invalidates_stale_approvals(self) -> None:
        ws = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws, "src/app.py", "--text", "print('v1')\n")
        self.tri("--actor", "agent:a", "run", ws, "--", sys.executable, "-c", "pass")
        cs_created = self.tri("--actor", "agent:a", "changeset", "create", ws, "--title", "Iterative fix")["data"]
        cs = cs_created["id"]
        head_v1 = cs_created["head_snapshot"]

        # Create a review with two assigned reviewers and a comment thread.
        rev = self.tri("review", "create", cs, "--reviewers", "human:r1", "--reviewers", "human:r2")["data"]["id"]
        self.tri("policy", "grant", cs, "--principal", "actor:human:r1", "--action", "changeset.write")
        self.tri("policy", "grant", cs, "--principal", "actor:human:r2", "--action", "changeset.write")
        comment = self.tri("--actor", "human:r1", "review", "comment", rev, "--body", "Please update v1 -> v2", "--path", "src/app.py", "--line", "1")["data"]
        self.tri("--actor", "human:r1", "review", "request-changes", rev, "--body", "Needs v2")
        self.assertEqual(self.tri("changeset", "inspect", cs)["data"]["status"], "changes_requested")

        # Edit workspace, run checks, and refresh the same changeset in place.
        self.tri("--actor", "agent:a", "files", "write", ws, "src/app.py", "--text", "print('v2')\n")
        self.tri("--actor", "agent:a", "run", ws, "--", sys.executable, "-c", "pass")
        refreshed = self.tri("--actor", "agent:a", "changeset", "refresh", cs, "--reason", "addressed-review")
        self.assertEqual(refreshed["schema"], "tri.changeset.refresh")
        self.assertEqual(len(refreshed["events"]), 1)
        rdata = refreshed["data"]
        self.assertTrue(rdata["updated"])
        self.assertEqual(rdata["id"], cs)
        self.assertEqual(rdata["previous_head_snapshot"], head_v1)
        self.assertNotEqual(rdata["head_snapshot"], head_v1)
        self.assertEqual(rdata["previous_status"], "changes_requested")
        self.assertEqual(rdata["status"], "ready")
        self.assertEqual(rdata["reopened_reviews"], [rev])
        self.assertEqual(rdata["lifecycle"]["refresh_count"], 1)

        # Review thread is preserved and review is reopened.
        rev_after = self.tri("review", "inspect", rev)["data"]
        self.assertEqual(rev_after["status"], "open")
        self.assertTrue(any(t["id"] == comment["action"] for t in rev_after["comment_threads"]) or rev_after["comment_threads"])

        # Idempotent no-op refresh emits no event.
        noop = self.tri("--actor", "agent:a", "changeset", "refresh", cs)
        self.assertFalse(noop["data"]["updated"])
        self.assertEqual(noop["events"], [])

        # Both reviewers approve v2 -> changeset becomes approved.
        self.tri("--actor", "human:r1", "review", "approve", rev)
        self.tri("--actor", "human:r2", "review", "approve", rev)
        self.assertEqual(self.tri("changeset", "inspect", cs)["data"]["status"], "approved")

        # Pushing new code (v3) and refreshing must invalidate BOTH prior approvals!
        self.tri("--actor", "agent:a", "files", "write", ws, "src/app.py", "--text", "print('v3')\n")
        self.tri("--actor", "agent:a", "run", ws, "--", sys.executable, "-c", "pass")
        refreshed_v3 = self.tri("--actor", "agent:a", "changeset", "refresh", cs, "--reason", "late-tweak")["data"]
        self.assertEqual(refreshed_v3["previous_status"], "approved")
        self.assertEqual(refreshed_v3["status"], "ready")
        self.assertEqual(refreshed_v3["reopened_reviews"], [rev])

        rev_v3 = self.tri("review", "inspect", rev)["data"]
        self.assertEqual(rev_v3["status"], "open")
        self.assertEqual(rev_v3["approved_reviewers"], [])
        self.assertEqual(rev_v3["pending_reviewers"], ["human:r1", "human:r2"])

        # Only one reviewer approving v3 is not enough; both must re-approve before merge.
        self.tri("--actor", "human:r1", "review", "approve", rev)
        self.assertEqual(self.tri("review", "inspect", rev)["data"]["pending_reviewers"], ["human:r2"])
        self.tri("--actor", "human:r2", "review", "approve", rev)
        self.tri("changeset", "merge", cs, "--target", "main")

        # Merged changesets cannot be refreshed.
        blocked = self.tri("--actor", "agent:a", "changeset", "refresh", cs, ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "changeset_lifecycle_blocked")

        # Unauthorized actor cannot refresh a private changeset.
        ws_priv = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "public")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", ws_priv, "src/app.py", "--text", "secret\n")
        cs_priv = self.tri("--actor", "agent:a", "changeset", "create", ws_priv, "--title", "PRIV_REFRESH_CANARY", "--visibility", "private")["data"]["id"]
        denied = self.tri("--actor", "agent:b", "changeset", "refresh", cs_priv, ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        self.tri("--actor", "agent:a", "files", "write", ws_priv, "src/app.py", "--text", "secret2\n")
        priv_evt = self.tri("--actor", "agent:a", "changeset", "refresh", cs_priv)["events"][0]
        self.assertEqual(self.tri("--actor", "agent:b", "events", "inspect", priv_evt, ok=False)["diagnostics"][0]["code"], "policy_denied")


if __name__ == "__main__":
    unittest.main()
