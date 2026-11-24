"""Tests for atomic multi-file `files write-batch` and high-concurrency batched HTTP runs."""

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request
from urllib.error import URLError

from tri import cli


def _post(url: str, route: str, payload: dict) -> dict:
    req = request.Request(
        f"{url}{route}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


class FilesWriteBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tri-batch-test-")
        self.root = Path(self._tmp.name)
        self.home = self.root / ".tri"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        self._prev_home = os.environ.get("TRI_HOME")
        os.environ["TRI_HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("TRI_HOME", None)
        else:
            os.environ["TRI_HOME"] = self._prev_home
        self._tmp.cleanup()

    def test_write_batch_commits_single_snapshot_and_operation(self) -> None:
        imp, status = cli.execute_argv(["import", str(self.repo), "--name", "demo"])
        self.assertEqual(status, 0, imp)
        target = imp["data"]["target"]["name"]

        ws, status = cli.execute_argv(["workspace", "create", "demo", "--from", target])
        self.assertEqual(status, 0, ws)
        ws_id = ws["data"]["id"]
        head0 = ws["data"]["head_snapshot"]

        entries = [
            {"path": "src/a.py", "base64": base64.b64encode(b"A = 1\n").decode("ascii")},
            {"path": "src/b.py", "text": "B = 2\n"},
            {"path": "src/sub/c.py", "base64": base64.b64encode(b"C = 3\n").decode("ascii")},
        ]
        res, status = cli.execute_argv(
            [
                "files",
                "write-batch",
                ws_id,
                "--entries-json",
                json.dumps(entries),
                "--expect-head",
                head0,
            ]
        )
        self.assertEqual(status, 0, res)
        self.assertEqual(res["schema"], "tri.files.write_batch")
        self.assertEqual(res["data"]["paths"], ["src/a.py", "src/b.py", "src/sub/c.py"])

        # Exactly one workspace mutation operation should be recorded for all 3 files.
        ops, status = cli.execute_argv(["operations", "list", ws_id])
        self.assertEqual(status, 0, ops)
        self.assertEqual(len(ops["data"]["operations"]), 1)
        self.assertEqual(ops["data"]["operations"][0]["kind"], "write_file_batch")

        # Stale expect-head is rejected atomically.
        stale, status = cli.execute_argv(
            [
                "files",
                "write-batch",
                ws_id,
                "--entries-json",
                json.dumps({"src/d.py": "D = 4\n"}),
                "--expect-head",
                head0,
            ]
        )
        self.assertNotEqual(status, 0)
        self.assertEqual(stale["diagnostics"][0]["code"], "stale_workspace_head")

    def test_concurrent_single_batch_lifecycle_over_http(self) -> None:
        imp, status = cli.execute_argv(["import", str(self.repo), "--name", "demo"])
        self.assertEqual(status, 0, imp)
        target = imp["data"]["target"]["name"]

        # Start tri serve on a free loopback port.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        env = dict(os.environ)
        env["TRI_HOME"] = str(self.home)
        env["TRI_AUTO_REAP"] = "0"
        proc = subprocess.Popen(
            [sys.executable, "-m", "tri", "serve", "--host", "127.0.0.1", "--port", str(port), "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.time() + 15.0
            while time.time() < deadline:
                try:
                    with request.urlopen(f"{base_url}/v0/health", timeout=2) as resp:
                        if resp.status == 200:
                            break
                except (URLError, ConnectionError, TimeoutError):
                    time.sleep(0.05)
            else:
                self.fail("tri serve did not become healthy")

            def run_one(i: int) -> tuple[bool, int, str]:
                files_payload = [
                    {
                        "path": "app.py",
                        "base64": base64.b64encode(f"VAL = {i}\n".encode()).decode("ascii"),
                    },
                    {
                        "path": "check.py",
                        "base64": base64.b64encode(
                            f"import app\nassert app.VAL == {i}\nprint('ok', app.VAL)\n".encode()
                        ).decode("ascii"),
                    },
                ]
                batch_resp = _post(
                    base_url,
                    "/v0/batch",
                    {
                        "commands": [
                            {"as": "ws", "argv": ["workspace", "create", "demo", "--from", target]},
                            {
                                "argv": [
                                    "files",
                                    "write-batch",
                                    "${ws.data.id}",
                                    "--entries-json",
                                    json.dumps(files_payload),
                                ]
                            },
                            {
                                "as": "run",
                                "argv": [
                                    "run",
                                    "${ws.data.id}",
                                    "--no-import-changes",
                                    "--no-env",
                                    "--",
                                    sys.executable,
                                    "check.py",
                                ],
                            },
                            {
                                "as": "stdout",
                                "argv": [
                                    "checks",
                                    "stream",
                                    "${run.data.id}",
                                    "--log",
                                    "stdout",
                                    "--limit",
                                    "65536",
                                ],
                            },
                            {"argv": ["workspace", "discard", "${ws.data.id}"]},
                        ]
                    },
                )
                run_data = batch_resp["data"]["results"][2]["data"]
                stdout = batch_resp["data"]["results"][3]["data"]["text"].strip()
                return bool(batch_resp["ok"]), int(run_data["exit_status"]), stdout

            with ThreadPoolExecutor(max_workers=16) as pool:
                outcomes = list(pool.map(run_one, range(32)))

            for i, (ok, rc, out) in enumerate(outcomes):
                self.assertTrue(ok, f"worker {i} batch failed")
                self.assertEqual(rc, 0, f"worker {i} exit {rc}")
                self.assertEqual(out, f"ok {i}")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
