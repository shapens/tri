"""Regional cache: content crosses the WAN once per region, authorization stays upstream."""

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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RegionalCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-regional-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        for index in range(5):
            (repo / "src" / f"f{index}.txt").write_text(f"content {index}\n" * 50)
        self.env = {**os.environ, "TRI_HOME": str(self.root / "server-home"), "TRI_AUTO_REAP": "0"}
        for key in ("TRI_REMOTE", "TRI_ACTOR", "TRI_TOKEN", "TRI_TOKEN_FILE"):
            self.env.pop(key, None)
        self.cli("import", str(repo), "--name", "app", "--target", "main")
        tokens = self.root / "tokens.json"
        self.token_a = self.cli("auth", "issue", "agent:a", "--file", str(tokens))["data"]["token"]
        self.token_b = self.cli("auth", "issue", "agent:b", "--file", str(tokens))["data"]["token"]
        upstream = free_port()
        self.spawn(["serve", "--port", str(upstream), "--quiet", "--auth-file", str(tokens)], f"http://127.0.0.1:{upstream}/v0/health")
        cache_port = free_port()
        self.spawn(["cache", "serve", "--upstream", f"http://127.0.0.1:{upstream}", "--port", str(cache_port), "--dir", str(self.root / "regional"), "--quiet"], f"http://127.0.0.1:{cache_port}/v0/cache/health")
        self.cache_url = f"http://127.0.0.1:{cache_port}"
        patcher = mock.patch.dict(os.environ, {"TRI_REMOTE": self.cache_url, "TRI_TOKEN": self.token_a, "TRI_HOME": str(self.root / "client-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)

    def cli(self, *args: str) -> dict:
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"], output)
        return output

    def spawn(self, args: list[str], health: str) -> None:
        proc = subprocess.Popen([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

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
                with request.urlopen(health, timeout=2) as response:
                    if response.status == 200:
                        return
            except (URLError, ConnectionError, TimeoutError):
                time.sleep(0.05)
        self.fail(f"{args[0]} did not start")

    def tri(self, *argv: str, token: str | None = None, cache: str = "machine-1", ok: bool = True) -> dict:
        with mock.patch.dict(os.environ, {"TRI_TOKEN": token or self.token_a, "TRI_CACHE_DIR": str(self.root / cache)}):
            output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        return output

    def health(self) -> dict:
        with request.urlopen(f"{self.cache_url}/v0/cache/health", timeout=5) as response:
            return json.loads(response.read())

    def test_second_machine_mount_is_served_from_the_region(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("workspace", "mount", ws, str(self.root / "m1"), cache="machine-1")
        self.assertEqual(self.health()["misses"], 5)
        self.tri("workspace", "mount", ws, str(self.root / "m2"), cache="machine-2")
        stats = self.health()
        self.assertEqual(stats["misses"], 5)
        self.assertEqual(stats["hits"], 5)
        self.assertEqual((self.root / "m2" / "src" / "f3.txt").read_text(), "content 3\n" * 50)

    def test_cached_content_still_requires_upstream_authorization(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("workspace", "mount", ws, str(self.root / "m1"))
        denied = self.tri("files", "fetch", ws, "--path", "src/f1.txt", token=self.token_b, ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")

    def test_writes_pass_through_to_the_service(self) -> None:
        ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]
        self.tri("files", "write", ws, "src/new.txt", "--text", "through the region\n")
        self.assertEqual(self.tri("files", "read", ws, "src/new.txt")["data"]["text"], "through the region\n")


if __name__ == "__main__":
    unittest.main()
