"""Remote mode: a central `tri serve` is the only store; clients keep caches."""

from __future__ import annotations

import base64
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
from tri.cli import SCHEMA_CATALOG  # noqa: E402


class RemoteModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-remote-")
        self.addCleanup(self.tmp.cleanup)  # registered first: runs after the server stops
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("print('v1')\n")
        (repo / "docs").mkdir()
        (repo / "docs" / "guide.md").write_text("guide\n")
        (repo / "logo.bin").write_bytes(bytes(range(256)))
        self.server_env = {**os.environ, "TRI_HOME": str(self.root / "server-home"), "TRI_AUTO_REAP": "0"}
        self.server_env.pop("TRI_REMOTE", None)
        self.server_env.pop("TRI_ACTOR", None)
        self.server_tri("import", str(repo), "--name", "app", "--target", "main")
        tokens = self.root / "tokens.json"
        self.token = self.server_tri("auth", "issue", "agent:a", "--file", str(tokens))["data"]["token"]
        self.port = self.start_server(tokens)
        self.url = f"http://127.0.0.1:{self.port}"
        self.client_home = self.root / "client-home"
        client_env = {"TRI_REMOTE": self.url, "TRI_TOKEN": self.token, "TRI_HOME": str(self.client_home), "TRI_CACHE_DIR": str(self.root / "client-cache")}
        patcher = mock.patch.dict(os.environ, client_env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)

    def server_tri(self, *args: str) -> dict:
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.server_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"], output)
        return output

    def start_server(self, tokens: Path) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "tri", "serve", "--port", str(port), "--quiet", "--auth-file", str(tokens)], cwd=ROOT, env=self.server_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

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
                with request.urlopen(f"http://127.0.0.1:{port}/v0/health", timeout=2) as response:
                    if response.status == 200:
                        return port
            except (URLError, ConnectionError, TimeoutError):
                time.sleep(0.05)
        self.fail("server did not start")

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

    def read(self, ref: str, path: str) -> str | None:
        output, _ = cli.execute_argv(["files", "read", ref, path])
        return output["data"].get("text") if output["ok"] else None

    def test_commands_run_on_the_server_and_the_client_keeps_no_store(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]
        self.assertEqual(ws["owner"] if "owner" in ws else "agent:a", "agent:a")
        self.tri("files", "write", ws["id"], "src/app.py", "--text", "print('v2')\n")
        self.assertEqual(self.read(ws["id"], "src/app.py"), "print('v2')\n")
        self.assertFalse(self.client_home.exists(), "remote mode must not create a local store")

    def test_local_body_files_are_uploaded_not_read_on_the_server(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        local = self.root / "local-only.txt"
        local.write_text("from the client\n")
        self.tri("files", "write", ws, "notes.txt", "--body", str(local))
        self.assertEqual(self.read(ws, "notes.txt"), "from the client\n")

    def test_raw_api_refuses_server_paths_for_agents(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        req = request.Request(f"{self.url}/v0/command", data=json.dumps({"argv": ["files", "write", ws, "stolen.txt", "--body", str(self.root / "tokens.json")]}).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, timeout=10) as response:
                body = json.loads(response.read())
        except HTTPError as exc:
            body = json.loads(exc.read())
        self.assertFalse(body["ok"])
        self.assertEqual(body["diagnostics"][0]["code"], "policy_denied")
        self.assertEqual(body["diagnostics"][0]["reason"], "server_path")
        self.assertIsNone(self.read(ws, "stolen.txt"))

    def test_mount_fetches_once_then_serves_from_the_machine_cache(self) -> None:
        first = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        mounted = self.tri("workspace", "mount", first, str(self.root / "m1"))["data"]
        self.assertEqual(mounted["files"], 3)
        self.assertGreater(mounted["fetched_bytes"], 0)
        self.assertEqual((self.root / "m1" / "logo.bin").read_bytes(), bytes(range(256)))
        second = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        again = self.tri("workspace", "mount", second, str(self.root / "m2"))["data"]
        self.assertEqual(again["fetched_bytes"], 0)
        self.assertEqual(again["cache_hits"], 3)

    def test_sync_sends_edits_deletes_and_new_files_in_one_batch(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        mount = self.root / "mnt"
        self.tri("workspace", "mount", ws, str(mount))
        (mount / "src" / "app.py").write_text("print('edited')\n")
        (mount / "docs" / "guide.md").unlink()
        (mount / "src" / "new.py").write_text("new\n")
        synced = self.tri("workspace", "sync", ws)["data"]
        self.assertEqual(synced["written"], ["src/app.py", "src/new.py"])
        self.assertEqual(synced["deleted"], ["docs/guide.md"])
        self.assertEqual(self.read(ws, "src/app.py"), "print('edited')\n")
        self.assertIsNone(self.read(ws, "docs/guide.md"))
        self.assertEqual(self.tri("workspace", "sync", ws)["data"]["written"], [])

    def test_sync_refuses_when_the_workspace_moved_on_the_server(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        mount = self.root / "mnt"
        self.tri("workspace", "mount", ws, str(mount))
        self.tri("files", "write", ws, "src/app.py", "--text", "server side\n")
        (mount / "src" / "app.py").write_text("client side\n")
        stale = self.tri("workspace", "sync", ws, ok=False)
        self.assertEqual(stale["diagnostics"][0]["code"], "stale_materialization")
        self.assertEqual(self.read(ws, "src/app.py"), "server side\n")

    def test_partial_remote_mount(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        mounted = self.tri("workspace", "mount", ws, str(self.root / "part"), "--paths", "src")["data"]
        self.assertEqual(mounted["files"], 1)
        self.assertFalse((self.root / "part" / "docs").exists())
        (self.root / "part" / "src" / "app.py").write_text("partial edit\n")
        self.tri("workspace", "sync", ws)
        self.assertEqual(self.read(ws, "docs/guide.md"), "guide\n")
        self.assertEqual(self.read(ws, "src/app.py"), "partial edit\n")

    def test_runs_execute_on_the_server(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        check = self.tri("run", ws, "--", sys.executable, "-c", "import os; open('where.txt','w').write(os.environ['TRI_HOME'])")["data"]
        self.assertEqual(check["status"], "passed")
        self.assertEqual(self.read(ws, "where.txt"), str(self.root / "server-home"))

    def test_mcp_calls_forward_with_the_token_identity(self) -> None:
        output, _ = cli.execute_argv(["workspace", "create", "app", "--from", "main"], pinned_actor="agent:a")
        self.assertTrue(output["ok"], output)
        self.assertEqual(output["actor"], "agent:a")
        mismatch, _ = cli.execute_argv(["--actor", "actor_local", "projects", "list"])
        self.assertEqual(mismatch["diagnostics"][0]["code"], "actor_mismatch")

    def test_unreachable_server_is_a_retryable_diagnostic(self) -> None:
        with mock.patch.dict(os.environ, {"TRI_REMOTE": "http://127.0.0.1:9"}):
            output = self.tri("projects", "list", ok=False)
        self.assertEqual(output["diagnostics"][0]["code"], "remote_unreachable")
        self.assertTrue(output["diagnostics"][0]["retryable"])

    def test_remote_files_fetch_returns_base64_and_hashes(self) -> None:
        snapshot = self.tri("targets", "list", "app")["data"]["targets"][0]["snapshot"]
        fetched = self.tri("files", "fetch", snapshot, "--path", "logo.bin", "--path", "missing.txt")["data"]
        self.assertEqual(base64.b64decode(fetched["files"][0]["content"]), bytes(range(256)))
        self.assertEqual(fetched["missing"], ["missing.txt"])


if __name__ == "__main__":
    unittest.main()
