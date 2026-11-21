"""Agent-scale gaps: line-level merges, merge rules, merge retries, history queries."""

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
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402
from tri.cli import SCHEMA_CATALOG, TriError  # noqa: E402
from tri.mcp import TriMCPServer  # noqa: E402
from tri.textmerge import merge_three_way  # noqa: E402

BASE = "a\nb\nc\nd\ne\n"


class TextMergeTests(unittest.TestCase):
    def test_disjoint_edits_merge(self) -> None:
        self.assertEqual(merge_three_way(BASE.encode(), b"A\nb\nc\nd\ne\n", b"a\nb\nc\nd\nE\n"), b"A\nb\nc\nd\nE\n")

    def test_identical_edits_merge(self) -> None:
        self.assertEqual(merge_three_way(BASE.encode(), b"A\nb\nc\nd\ne\n", b"A\nb\nc\nd\ne\n"), b"A\nb\nc\nd\ne\n")

    def test_insertions_at_both_ends_merge(self) -> None:
        self.assertEqual(merge_three_way(BASE.encode(), (BASE + "f\n").encode(), ("z\n" + BASE).encode()), ("z\n" + BASE + "f\n").encode())

    def test_overlapping_edits_conflict(self) -> None:
        self.assertIsNone(merge_three_way(BASE.encode(), b"A\nb\nc\nd\ne\n", b"X\nb\nc\nd\ne\n"))

    def test_adjacent_edits_conflict(self) -> None:
        self.assertIsNone(merge_three_way(BASE.encode(), b"a\nB\nc\nd\ne\n", b"a\nb\nC\nd\ne\n"))

    def test_binary_never_merges(self) -> None:
        self.assertIsNone(merge_three_way(b"a\0\n", b"b\0\n", b"a\0\nc\n"))


class AgentNativeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-agent-native-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "tri@example.com")
        self.git("config", "user.name", "Tri Test")
        (self.repo / "docs").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "docs" / "guide.md").write_text(BASE)
        (self.repo / "src" / "app.txt").write_text("one\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.env = os.environ.copy()
        self.env["TRI_HOME"] = str(self.root / "tri-home")
        self.env.pop("TRI_ACTOR", None)
        self.tri("import", str(self.repo), "--name", "app")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, stdout=subprocess.PIPE)

    def tri(self, *args: str, ok: bool | None = True) -> dict:
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        response = json.loads(result.stdout)
        if ok is None:
            pass
        elif ok:
            self.assertTrue(response["ok"], f"tri {' '.join(args)} failed: {result.stdout}\n{result.stderr}")
        else:
            self.assertFalse(response["ok"], f"tri {' '.join(args)} unexpectedly passed: {result.stdout}")
        advertised = set(SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        for diagnostic in response["diagnostics"]:
            self.assertIn(diagnostic["code"], advertised)
        schema = SCHEMA_CATALOG.get(response["schema"])
        if schema and response["ok"]:
            missing = [key for key in schema.get("required", []) if key not in response["data"]]
            self.assertEqual(missing, [], f"{response['schema']} missing {missing}")
        return response

    def workspace(self, *, actor: str | None = None) -> str:
        args = ["--actor", actor] if actor else []
        return self.tri(*args, "workspace", "create", "app", "--from", "main")["data"]["id"]

    def checked_changeset(self, workspace: str, path: str, text: str, *, actor: str | None = None) -> str:
        args = ["--actor", actor] if actor else []
        self.tri(*args, "files", "write", workspace, path, "--text", text)
        self.tri(*args, "run", workspace, "--", sys.executable, "-c", "pass")
        return self.tri(*args, "changeset", "create", workspace, "--title", f"edit {path}")["data"]["id"]

    def approve(self, changeset: str) -> None:
        review = self.tri("review", "create", changeset)["data"]["id"]
        self.tri("review", "approve", review)

    def target_snapshot(self) -> str:
        return self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]

    def target_id(self) -> str:
        return self.tri("targets", "list", "app")["data"]["targets"][0]["id"]

    def read(self, ref: str, path: str) -> str:
        return self.tri("files", "read", ref, path)["data"]["text"]

    # Line-level merges ------------------------------------------------------

    def test_changeset_merge_combines_disjoint_line_edits_to_one_file(self) -> None:
        first = self.checked_changeset(self.workspace(), "docs/guide.md", "A\nb\nc\nd\ne\n")
        second = self.checked_changeset(self.workspace(), "docs/guide.md", "a\nb\nc\nd\nE\n")
        self.approve(first)
        self.approve(second)
        self.assertEqual(self.tri("changeset", "merge", first, "--target", "main")["data"]["mode"], "fast_forward")
        merged = self.tri("changeset", "merge", second, "--target", "main")["data"]
        self.assertEqual(merged["mode"], "merge")
        self.assertEqual(merged["text_merged_paths"], ["docs/guide.md"])
        self.assertEqual(self.read(self.target_snapshot(), "docs/guide.md"), "A\nb\nc\nd\nE\n")

    def test_workspace_update_line_merges_instead_of_conflicting(self) -> None:
        workspace = self.workspace()
        self.tri("files", "write", workspace, "docs/guide.md", "--text", "a\nb\nc\nd\nE\n")
        (self.repo / "docs" / "guide.md").write_text("A\nb\nc\nd\ne\n")
        self.git("commit", "-am", "target edit")
        self.tri("targets", "refresh", "app", "main")
        updated = self.tri("workspace", "update", workspace, "--from", "main")["data"]
        self.assertEqual(updated["text_merged_paths"], ["docs/guide.md"])
        self.assertEqual(self.read(workspace, "docs/guide.md"), "A\nb\nc\nd\nE\n")

    # Merge rules -------------------------------------------------------------

    def test_merge_rule_lets_checked_agent_changeset_merge_without_review(self) -> None:
        agent = "agent:docs"
        changeset = self.checked_changeset(self.workspace(actor=agent), "docs/guide.md", "A\nb\nc\nd\ne\n", actor=agent)
        blocked = self.tri("--actor", agent, "changeset", "merge", changeset, "--target", "main", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_review_approval")

        denied = self.tri("--actor", agent, "merge-rules", "add", "app", "docs", "--path", "docs/**", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        rule = self.tri("merge-rules", "add", "app", "docs", "--path", "docs/*", "--max-files", "5")["data"]
        self.assertEqual(rule["allow"], ["role:agent", "role:human", "role:maintainer", "role:service"])

        merged = self.tri("--actor", agent, "changeset", "merge", changeset, "--target", "main")["data"]
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["merge_rule"], rule["id"])
        self.assertEqual(self.tri("merge-rules", "list", "app")["data"]["merge_rules"][0]["id"], rule["id"])

    def test_merge_rule_does_not_cover_paths_outside_its_globs_or_failing_checks(self) -> None:
        self.tri("merge-rules", "add", "app", "docs", "--path", "docs/*")
        outside = self.checked_changeset(self.workspace(), "src/app.txt", "two\n")
        blocked = self.tri("changeset", "merge", outside, "--target", "main", ok=False)
        self.assertIn("missing_review_approval", [d["code"] for d in blocked["diagnostics"]])

        workspace = self.workspace()
        self.tri("files", "write", workspace, "docs/guide.md", "--text", "changed\n")
        self.assertEqual(self.tri("run", workspace, "--", sys.executable, "-c", "raise SystemExit(1)")["data"]["status"], "failed")
        failing = self.tri("changeset", "create", workspace, "--title", "failing")["data"]["id"]
        blocked = self.tri("changeset", "merge", failing, "--target", "main", ok=False)
        codes = [d["code"] for d in blocked["diagnostics"]]
        self.assertIn("missing_required_check", codes)
        self.assertNotIn("missing_review_approval", codes)

    def test_removed_merge_rule_requires_review_again(self) -> None:
        self.tri("merge-rules", "add", "app", "docs", "--path", "docs/*")
        self.tri("merge-rules", "remove", "app", "docs")
        changeset = self.checked_changeset(self.workspace(), "docs/guide.md", "x\n")
        blocked = self.tri("changeset", "merge", changeset, "--target", "main", ok=False)
        self.assertEqual(blocked["diagnostics"][0]["code"], "missing_review_approval")
        self.assertEqual(self.tri("merge-rules", "list", "app")["data"]["merge_rules"], [])
        self.assertEqual(self.tri("merge-rules", "list", "app", "--all")["data"]["merge_rules"][0]["status"], "removed")

    # History queries ---------------------------------------------------------

    def test_files_log_and_blame_attribute_lines_to_changesets_without_checkout(self) -> None:
        first = self.checked_changeset(self.workspace(actor="agent:a"), "docs/guide.md", "A\nb\nc\nd\ne\n", actor="agent:a")
        self.approve(first)
        self.tri("changeset", "merge", first, "--target", "main")
        second = self.checked_changeset(self.workspace(actor="agent:b"), "docs/guide.md", "A\nb\nc\nd\nE\n", actor="agent:b")
        self.approve(second)
        self.tri("changeset", "merge", second, "--target", "main")

        target = self.target_id()
        log = self.tri("files", "log", target, "docs/guide.md")["data"]
        self.assertEqual([entry["change"] for entry in log["entries"]], ["modified", "modified", "added"])
        self.assertEqual(log["entries"][-1]["source"], "imported")
        self.assertIsNone(log["truncated"])

        blame = self.tri("files", "blame", target, "docs/guide.md")["data"]
        self.assertEqual(blame["line_count"], 5)
        by_line = {}
        for item in blame["ranges"]:
            for line in range(item["start"], item["end"] + 1):
                by_line[line] = item
        self.assertEqual(by_line[1]["created_by"], "agent:a")
        self.assertEqual(by_line[5]["created_by"], "agent:b")
        self.assertEqual(by_line[3]["source"], "imported")

    def test_files_log_refuses_hidden_paths(self) -> None:
        workspace = self.tri("workspace", "create", "app", "--from", "main", "--hide-path", "src/*")["data"]["id"]
        denied = self.tri("--actor", "agent:other", "files", "log", workspace, "src/app.txt", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        secret = self.tri("files", "log", workspace, ".env", ok=False)
        self.assertEqual(secret["diagnostics"][0]["code"], "policy_denied")

    def test_history_stops_at_snapshots_the_actor_cannot_read(self) -> None:
        workspace = self.tri("--actor", "agent:a", "workspace", "create", "app", "--from", "main", "--visibility", "private")["data"]["id"]
        self.tri("--actor", "agent:a", "files", "write", workspace, "docs/guide.md", "--text", "A\nb\nc\nd\ne\n")
        self.tri("--actor", "agent:a", "files", "write", workspace, "docs/guide.md", "--text", "A\nb\nc\nd\nE\n")
        self.tri("--actor", "agent:a", "run", workspace, "--", sys.executable, "-c", "pass")
        changeset = self.tri("--actor", "agent:a", "changeset", "create", workspace, "--title", "private edit")["data"]["id"]
        self.approve(changeset)
        self.tri("changeset", "merge", changeset, "--target", "main")
        target = self.target_id()

        owner_view = self.tri("files", "log", target, "docs/guide.md")["data"]
        self.assertIsNone(owner_view["truncated"])
        outsider = self.tri("--actor", "agent:other", "files", "log", target, "docs/guide.md")["data"]
        self.assertEqual(outsider["truncated"]["reason"], "policy_denied")
        self.assertLess(outsider["walked"], owner_view["walked"])
        blame = self.tri("--actor", "agent:other", "files", "blame", target, "docs/guide.md")["data"]
        self.assertTrue(all(item.get("redacted") for item in blame["ranges"]), blame["ranges"])
        self.assertIsNone(blame["boundary"])

    def test_merge_base_of_two_workspaces_is_their_shared_target_snapshot(self) -> None:
        base = self.target_snapshot()
        left = self.workspace()
        right = self.workspace()
        self.tri("files", "write", left, "docs/guide.md", "--text", "left\n")
        self.tri("files", "write", right, "src/app.txt", "--text", "right\n")
        data = self.tri("snapshots", "merge-base", left, right)["data"]
        self.assertEqual(data["merge_base"], base)
        self.assertEqual(data["left_distance"], 1)
        self.assertEqual(data["right_distance"], 1)

    def test_merge_retries_when_target_moves_during_merge(self) -> None:
        changeset = self.checked_changeset(self.workspace(), "docs/guide.md", "A\nb\nc\nd\ne\n")
        self.approve(changeset)
        real = cli.changeset_merge_once
        calls = {"count": 0}

        def flaky(store, args, command, start):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TriError("stale_target_snapshot", "Target changed while merging changeset.", retryable=True)
            return real(store, args, command, start)

        env = {"TRI_HOME": self.env["TRI_HOME"]}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "changeset_merge_once", flaky):
            output, status = cli.execute_argv(["changeset", "merge", changeset, "--target", "main"])
            self.assertEqual(status, 0, output)
            self.assertEqual(output["data"]["merge_attempts"], 2)
            calls["count"] = 0
            changeset_two = self.checked_changeset(self.workspace(), "src/app.txt", "two\n")
            self.approve(changeset_two)
            output, status = cli.execute_argv(["changeset", "merge", changeset_two, "--target", "main", "--retry", "0"])
            self.assertEqual(output["diagnostics"][0]["code"], "stale_target_snapshot")


class AuthenticatedServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-auth-")
        # Cleanups run last-in first-out: servers registered later stop before this
        # directory is removed. Windows cannot delete a SQLite file a live server holds.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env["TRI_HOME"] = str(self.root / "tri-home")
        self.env.pop("TRI_ACTOR", None)
        self.tokens = self.root / "tokens.json"

    def tri(self, *args: str) -> dict:
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return json.loads(result.stdout)

    def call(self, port: int, path: str, payload: dict | None = None, token: str | None = None, headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    @staticmethod
    def stop_server(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)

    def start_server(self, *extra: str) -> tuple[subprocess.Popen, int]:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "tri", "serve", "--host", "127.0.0.1", "--port", str(port), "--quiet", *extra], cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_server, proc)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                status, health = self.call(port, "/v0/health")
                if status == 200:
                    return proc, port
            except (ConnectionError, URLError, TimeoutError):
                time.sleep(0.05)
        self.fail("server did not start")

    def test_tokens_bind_requests_to_one_actor(self) -> None:
        issued = self.tri("auth", "issue", "agent:a", "--file", str(self.tokens), "--label", "ci")
        self.assertTrue(issued["ok"], issued)
        token = issued["data"]["token"]
        self.assertNotIn(token, self.tokens.read_text())
        self.assertEqual(self.tri("--actor", "agent:a", "auth", "issue", "agent:b", "--file", str(self.tokens))["diagnostics"][0]["code"], "policy_denied")

        _, port = self.start_server("--auth-file", str(self.tokens))
        status, body = self.call(port, "/v0/command", {"argv": ["projects", "list"]})
        self.assertEqual((status, body["diagnostics"][0]["code"]), (401, "unauthenticated"))
        status, body = self.call(port, "/v0/command", {"argv": ["projects", "list"]}, token="tri_wrong")
        self.assertEqual(status, 401)
        status, body = self.call(port, "/v0/health")
        self.assertEqual((status, body["data"]["auth"], body["data"]["home"]), (200, "bearer", None))

        status, body = self.call(port, "/v0/command", {"argv": ["actors", "inspect", "agent:a"]}, token=token)
        self.assertEqual((status, body["ok"], body["actor"]), (200, True, "agent:a"))
        for payload in (
            {"argv": ["--actor", "actor_local", "projects", "list"]},
            {"actor": "actor_local", "argv": ["projects", "list"]},
        ):
            status, body = self.call(port, "/v0/command", payload, token=token)
            self.assertEqual(body["diagnostics"][0]["code"], "actor_mismatch", payload)
        status, body = self.call(port, "/v0/batch", {"commands": [{"argv": ["projects", "list"], "actor": "actor_local"}]}, token=token)
        self.assertEqual(body["diagnostics"][0]["code"], "actor_mismatch")
        status, body = self.call(port, "/v0/commands", token=token, headers={"X-Tri-Actor": "actor_local"})
        self.assertEqual((status, body["diagnostics"][0]["code"]), (403, "actor_mismatch"))
        status, body = self.call(port, "/v0/command", {"argv": ["auth", "list", "--file", str(self.tokens)]}, token=token)
        self.assertEqual(body["diagnostics"][0]["code"], "invalid_command")

        self.assertTrue(self.tri("auth", "revoke", "agent:a", "--file", str(self.tokens))["ok"])
        status, _ = self.call(port, "/v0/command", {"argv": ["projects", "list"]}, token=token)
        self.assertEqual(status, 401)

    def test_serve_refuses_unauthenticated_non_loopback_bind(self) -> None:
        refused = self.tri("serve", "--host", "0.0.0.0", "--port", "0", "--quiet")
        self.assertEqual(refused["diagnostics"][0]["code"], "unauthenticated")


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-mcp-")
        self.env = os.environ.copy()
        self.env["TRI_HOME"] = str(Path(self.tmp.name) / "tri-home")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def exchange(self, *messages: dict) -> dict:
        lines = "".join(json.dumps(message) + "\n" for message in messages)
        result = subprocess.run([sys.executable, "-m", "tri", "--actor", "agent:claude", "mcp"], cwd=ROOT, env=self.env, input=lines, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        return {reply["id"]: reply for reply in replies}

    def test_stdio_handshake_tools_and_pinned_actor(self) -> None:
        replies = self.exchange(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "tri", "arguments": {"argv": ["actors", "inspect", "agent:claude"]}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "tri", "arguments": {"argv": ["--actor", "actor_local", "projects", "list"]}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "tri_batch", "arguments": {"commands": [{"argv": ["projects", "list"]}]}}},
            {"jsonrpc": "2.0", "id": 6, "method": "nope"},
        )
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5, 6])
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([tool["name"] for tool in replies[2]["result"]["tools"]], ["tri", "tri_batch"])
        inspect = replies[3]["result"]
        self.assertFalse(inspect["isError"])
        self.assertEqual(inspect["structuredContent"]["actor"], "agent:claude")
        self.assertEqual(json.loads(inspect["content"][0]["text"])["schema"], "tri.actor.inspect")
        self.assertEqual(replies[4]["result"]["structuredContent"]["diagnostics"][0]["code"], "actor_mismatch")
        self.assertTrue(replies[5]["result"]["structuredContent"]["ok"])
        self.assertEqual(replies[6]["error"]["code"], -32601)

    def test_unknown_tool_is_invalid_params(self) -> None:
        server = TriMCPServer(actor="agent:x", execute=lambda argv: {"ok": True}, batch=lambda payload: {"ok": True})
        reply = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
        self.assertEqual(reply["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
