"""tri-server against tri serve: the same requests on the same store must give the
same responses, and the native runtime's own duties (readiness, metrics, worker
replacement, timeouts, drain, startup refusals) must work with real workers.

Runs only when TRI_CONFORMANCE_NATIVE names a tri-server binary.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from .harness import ROOT, Contracts, Server, base_env, cli_command, free_port, make_repo, native_binary, run_cli

NATIVE = native_binary()
AGENT = "agent:conformance"


def normalize(value: Any, ports: tuple[int, int]) -> Any:
    """Drop timing, and the port each server reports for itself."""
    if isinstance(value, dict):
        return {key: normalize(item, ports) for key, item in value.items() if key not in ("cost", "wall_ms")}
    if isinstance(value, list):
        return [normalize(item, ports) for item in value]
    if isinstance(value, str):
        return value.replace(f":{ports[0]}", ":PORT").replace(f":{ports[1]}", ":PORT")
    if value in ports and not isinstance(value, bool):
        return "PORT"
    return value


@unittest.skipUnless(NATIVE, "set TRI_CONFORMANCE_NATIVE to a tri-server binary")
class NativeDifferentialTests(unittest.TestCase):
    """Both servers on one home, with and without tokens."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = Contracts()
        cls.tmp = Path(tempfile.mkdtemp(prefix="tri-conformance-native-"))
        cls.env = base_env(cls.tmp / "home")
        repo = make_repo(cls.tmp)
        status, _, err = run_cli(["import", str(repo), "--name", "app"], cls.env)
        assert status == 0, err
        _, ws, _ = run_cli(["workspace", "create", "app", "--from", "main", "--actor", AGENT], cls.env)
        cls.ws = ws["data"]["id"]
        run_cli(["files", "write", cls.ws, "src/app.py", "--text", "def main():\n    return 'changed'\n", "--actor", AGENT], cls.env)
        _, cs, _ = run_cli(["changeset", "create", cls.ws, "--title", "differential", "--actor", AGENT], cls.env)
        cls.cs = cs["data"]["id"]
        cls.token_file = cls.tmp / "tokens.json"
        cls.tokens = {}
        for actor in ("actor_local", AGENT, "agent:other"):
            _, issued, _ = run_cli(["auth", "issue", actor, "--file", str(cls.token_file)], cls.env)
            cls.tokens[actor] = issued["data"]["token"]
        cls.servers: list[Server] = []
        try:
            cls.python_open = cls.track(Server.start(cls.env, native=""))
            cls.native_open = cls.track(Server.start(cls.env, native=NATIVE))
            cls.python_auth = cls.track(Server.start(cls.env, str(cls.token_file), native=""))
            cls.native_auth = cls.track(Server.start(cls.env, str(cls.token_file), native=NATIVE))
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def track(cls, server: Server) -> Server:
        cls.servers.append(server)
        return server

    @classmethod
    def tearDownClass(cls) -> None:
        for server in cls.servers:
            server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def same(self, python: Server, native: Server, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> None:
        a = python.request(method, path, body, headers)
        b = native.request(method, path, body, headers)
        self.assertEqual(b.status, a.status, f"{method} {path}: {b.text[:300]}")
        ports = (python.port, native.port)
        self.assertEqual(normalize(b.json, ports), normalize(a.json, ports), f"{method} {path}")
        summary = isinstance(b.json, dict) and "command" not in b.json
        self.assertEqual(self.contracts.envelope_errors(b.json, summary=summary), [])

    def test_reads_match(self) -> None:
        paths = [
            "/v0", "/v0/health", "/health", "/v0/commands", "/v0/schema", "/v0/schema/tri.diff", "/v0/projects", "/v0/projects/app",
            "/v0/projects/app/targets", "/v0/projects/app/workspaces", f"/v0/workspaces/{self.ws}", f"/v0/workspaces/{self.ws}/files",
            f"/v0/workspaces/{self.ws}/files/src/app.py", f"/v0/workspaces/{self.ws}/diff?mode=patch", f"/v0/changesets/{self.cs}",
            f"/v0/objects/{self.cs}", "/v0/no/such/route", "/v0/schema/tri.nope",
        ]
        commands = [
            ["files", "read", self.ws, "src/app.py"], ["diff", self.cs], ["workspace", "list"], ["--response", "summary", "workspace", "list"],
            ["files", "search", self.ws, "changed"], ["workspace", "inspect", "ws_missing00000"], ["serve"], ["worker", "--stdio"], ["files"],
        ]
        for path in paths:
            with self.subTest(path=path):
                self.same(self.python_open, self.native_open, "GET", path)
                self.same(self.python_auth, self.native_auth, "GET", path, headers={"Authorization": f"Bearer {self.tokens[AGENT]}"})
        for argv in commands:
            with self.subTest(argv=argv):
                body = json.dumps({"argv": argv, "actor": AGENT}).encode()
                self.same(self.python_open, self.native_open, "POST", "/v0/command", body)
                self.same(self.python_auth, self.native_auth, "POST", "/v0/command", json.dumps({"argv": argv}).encode(), {"Authorization": f"Bearer {self.tokens[AGENT]}"})
        batch = json.dumps({"commands": [{"argv": ["workspace", "inspect", self.ws], "as": "w"}, {"argv": ["files", "list", "${w.data.id}"]}], "return": {"id": "${w.data.id}"}}).encode()
        self.same(self.python_open, self.native_open, "POST", "/v0/batch", batch)

    def test_refusals_match(self) -> None:
        agent = {"Authorization": f"Bearer {self.tokens[AGENT]}"}
        server_file = self.tmp / "server.txt"
        server_file.write_bytes(b"server-local\n")
        cases = [
            ("GET", "/v0/projects", None, {}),
            ("GET", "/v0/projects", None, {"Authorization": "Bearer tri_wrong"}),
            ("POST", "/v0/command", b'{"argv": ["projects", "list"]}', {}),
            ("GET", "/v0/projects", None, {**agent, "X-Tri-Actor": "agent:other"}),
            ("POST", "/v0/command", json.dumps({"argv": ["projects", "list"], "actor": "agent:other"}).encode(), agent),
            ("POST", "/v0/command", json.dumps({"argv": ["files", "write", self.ws, "x", "--body", str(server_file)]}).encode(), agent),
            ("POST", "/v0/batch", json.dumps({"commands": [{"argv": ["import", str(self.tmp), "--name", "x"]}]}).encode(), agent),
            ("POST", "/v0/command", b"not json", agent),
            ("POST", "/v0/command", b"[]", agent),
            ("POST", "/v0/batch", b'{"commands": [{"argv": ["auth", "list", "--file", "x"]}]}', agent),
            ("POST", "/v0/elsewhere", b"{}", agent),
            ("GET", "/v0/health", None, {"X-Tri-Actor": "agent:other"}),
        ]
        for method, path, body, headers in cases:
            with self.subTest(method=method, path=path, headers=sorted(headers), body=body):
                self.same(self.python_auth, self.native_auth, method, path, body, headers)

    def test_ready_and_metrics(self) -> None:
        ready = self.native_open.get("/v0/ready")
        self.assertEqual(ready.status, 200)
        self.assertTrue(ready.json["data"]["ready"])
        self.assertGreaterEqual(ready.json["data"]["workers"]["live"], 1)
        self.native_open.command(["projects", "list"])
        metrics = self.native_open.get("/metrics")
        self.assertEqual(metrics.status, 200)
        for name in ("tri_http_requests_total", "tri_http_request_duration_seconds_bucket", "tri_worker_duration_seconds_bucket", "tri_queue_wait_seconds_count", "tri_workers{state=\"idle\"}", "tri_ready 1"):
            self.assertIn(name, metrics.text)
        self.assertIn('route="command:projects"', metrics.text)
        self.assertEqual(self.native_auth.get("/metrics").status, 401)
        self.assertEqual(self.native_auth.get("/metrics", token=self.tokens[AGENT]).status, 200)


@unittest.skipUnless(NATIVE, "set TRI_CONFORMANCE_NATIVE to a tri-server binary")
class NativeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tri-conformance-runtime-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = base_env(self.tmp / "home")
        repo = make_repo(self.tmp)
        status, _, err = run_cli(["import", str(repo), "--name", "app"], self.env)
        self.assertEqual(status, 0, err)
        _, ws, _ = run_cli(["workspace", "create", "app", "--from", "main"], self.env)
        self.ws = ws["data"]["id"]

    def start(self, *extra: str) -> tuple[subprocess.Popen, int]:
        port = free_port()
        proc = subprocess.Popen([NATIVE, "--host", "127.0.0.1", "--port", str(port), "--quiet", "--workers", "1", "--max-workers", "2", *extra, "--", *cli_command(), "worker", "--stdio"], cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        server = Server(f"http://127.0.0.1:{port}")
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if server.get("/v0/ready").status == 200:
                    return proc, port
            except OSError:
                time.sleep(0.05)
        self.fail("tri-server did not become ready")

    def sleep_argv(self, seconds: float) -> list[str]:
        return ["run", self.ws, "--", sys.executable, "-c", f"import time; time.sleep({seconds})"]

    def test_timeout_kills_the_worker_and_a_new_one_serves(self) -> None:
        _, port = self.start("--request-timeout", "2s")
        server = Server(f"http://127.0.0.1:{port}")
        began = time.time()
        timed_out = server.command(self.sleep_argv(30))
        self.assertEqual(timed_out.status, 504, timed_out.text[:300])
        self.assertEqual(timed_out.codes(), ["request_timeout"])
        self.assertLess(time.time() - began, 20)
        after = server.command(["projects", "list"])
        self.assertEqual(after.status, 200)
        self.assertTrue(after.json["ok"])
        self.assertGreaterEqual(server.get("/v0/ready").json["data"]["workers"]["restarts"], 1)

    @unittest.skipIf(os.name == "nt", "graceful drain is signal-driven; Windows has no SIGTERM")
    def test_sigterm_drains_in_flight_requests(self) -> None:
        proc, port = self.start("--drain-timeout", "60s")
        server = Server(f"http://127.0.0.1:{port}")
        result: dict[str, Any] = {}
        thread = threading.Thread(target=lambda: result.update(response=server.command(self.sleep_argv(2))))
        thread.start()
        time.sleep(0.8)
        proc.send_signal(signal.SIGTERM)
        thread.join(timeout=60)
        response = result["response"]
        self.assertEqual(response.status, 200, response.text[:300])
        self.assertTrue(response.json["ok"], response.text[:300])
        self.assertEqual(proc.wait(timeout=30), 0)
        with self.assertRaises(OSError):
            server.get("/v0/health")

    def test_startup_refusals_match_tri_serve(self) -> None:
        port = free_port()
        native = subprocess.run([NATIVE, "--host", "0.0.0.0", "--port", str(port), "--", *cli_command(), "worker", "--stdio"], cwd=ROOT, env=self.env, capture_output=True, timeout=60)
        python = subprocess.run([*cli_command(), "serve", "--host", "0.0.0.0", "--port", str(port)], cwd=ROOT, env=self.env, capture_output=True, timeout=60)
        self.assertEqual(native.returncode, 1, native.stderr[-500:])
        self.assertEqual(python.returncode, 1)
        native_body, python_body = json.loads(native.stdout), json.loads(python.stdout)
        self.assertEqual(native_body["diagnostics"], python_body["diagnostics"])
        self.assertEqual(native_body["diagnostics"][0]["code"], "unauthenticated")


if __name__ == "__main__":
    unittest.main()
