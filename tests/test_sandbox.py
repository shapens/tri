"""Sandboxed runs: no Tri home or credential reads, no stray writes, no network, limits."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli, sandbox  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402

ISOLATED = sandbox.probe()["isolated"]

PROBE_SCRIPT = r"""
import os, socket, sys
results = {}
def attempt(name, fn):
    try:
        fn()
        results[name] = "allowed"
    except Exception as exc:
        results[name] = "blocked"
attempt("read_store", lambda: open(sys.argv[1], "rb").read(1))
attempt("list_home", lambda: os.listdir(sys.argv[2]))
attempt("write_outside", lambda: open(sys.argv[3], "w").write("x"))
attempt("network", lambda: socket.create_connection(("127.0.0.1", int(sys.argv[4])), timeout=2).close())
results["env_secret"] = "visible" if os.environ.get("TRI_TEST_CANARY_SECRET") else "hidden"
open("probe.txt", "w").write(repr(sorted(results.items())))
"""


class SandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-sandbox-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "app.txt").write_text("app\n")
        self.home = self.root / "tri-home"
        env = {"TRI_HOME": str(self.home), "TRI_AUTO_REAP": "0", "TRI_TEST_CANARY_SECRET": "canary-value"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        os.environ.pop(sandbox.MODE_ENV, None)
        self.tri("import", str(repo), "--name", "app", "--target", "main")
        self.ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]

    def tri(self, *argv: str, ok: bool = True) -> dict:
        output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        for diagnostic in output["diagnostics"]:
            self.assertIn(diagnostic["code"], SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        return output

    def read(self, path: str) -> str | None:
        output, _ = cli.execute_argv(["files", "read", self.ws, path])
        return output["data"].get("text") if output["ok"] else None

    def listener(self) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.addCleanup(server.close)

        def accept() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                    conn.close()
                except OSError:
                    return

        threading.Thread(target=accept, daemon=True).start()
        return server.getsockname()[1]

    def probe(self, *flags: str) -> tuple[dict, dict[str, str]]:
        port = self.listener()
        outside = self.root / "outside.txt"
        check = self.tri("run", self.ws, *flags, "--", sys.executable, "-c", PROBE_SCRIPT, str(self.home / "tri.sqlite3"), str(self.home), str(outside), str(port))["data"]
        return check, dict(eval(self.read("probe.txt") or "[]"))  # noqa: S307 - written by the probe above

    def test_runs_are_unsandboxed_by_default_on_a_workstation(self) -> None:
        check, results = self.probe()
        self.assertEqual(check["environment_policy"]["sandbox"]["kind"], "off")
        self.assertEqual(results["read_store"], "allowed")
        self.assertEqual(results["env_secret"], "visible")

    @unittest.skipUnless(ISOLATED, "this host has no sandbox backend")
    def test_sandboxed_run_cannot_read_the_store_write_outside_or_reach_the_network(self) -> None:
        check, results = self.probe("--sandbox")
        policy = check["environment_policy"]["sandbox"]
        self.assertTrue(policy["isolated"], policy)
        self.assertEqual(results["read_store"], "blocked")
        self.assertEqual(results["list_home"], "blocked")
        self.assertEqual(results["write_outside"], "blocked")
        self.assertEqual(results["network"], "blocked")
        self.assertEqual(results["env_secret"], "hidden")
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual(check["status"], "passed")  # writes inside the run directory still import

    @unittest.skipUnless(ISOLATED, "this host has no sandbox backend")
    def test_server_mode_sandboxes_every_run(self) -> None:
        with mock.patch.dict(os.environ, {sandbox.MODE_ENV: "required"}):
            check, results = self.probe()
        self.assertTrue(check["environment_policy"]["sandbox"]["isolated"])
        self.assertEqual(results["read_store"], "blocked")

    @unittest.skipIf(os.name == "nt", "process-group kill is POSIX")
    def test_timeout_kills_the_process_group(self) -> None:
        started = time.time()
        with mock.patch.dict(os.environ, {sandbox.MODE_ENV: "auto"}):
            check = self.tri("run", self.ws, "--timeout", "1", "--", sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")["data"]
        self.assertLess(time.time() - started, 30)
        self.assertEqual(check["status"], "failed")
        self.assertTrue(check["environment_policy"]["sandbox"]["timed_out"])

    def test_required_mode_refuses_on_a_host_without_isolation(self) -> None:
        with mock.patch.object(sandbox, "probe", return_value={"kind": "none", "isolated": False, "reason": "test host"}):
            denied = self.tri("run", self.ws, "--sandbox", "--", sys.executable, "-c", "open('ran.txt','w').write('x')", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "sandbox_unavailable")
        self.assertIsNone(self.read("ran.txt"))

    def test_serving_beyond_loopback_requires_a_sandbox(self) -> None:
        tokens = self.root / "tokens.json"
        self.tri("auth", "issue", "agent:a", "--file", str(tokens))
        with mock.patch.object(sandbox, "probe", return_value={"kind": "none", "isolated": False, "reason": "test host"}):
            refused = self.tri("serve", "--host", "0.0.0.0", "--port", "0", "--auth-file", str(tokens), "--quiet", ok=False)
        self.assertEqual(refused["diagnostics"][0]["code"], "sandbox_unavailable")

    def test_doctor_reports_the_sandbox_backend(self) -> None:
        self.assertIn(self.tri("doctor")["data"]["sandbox"]["kind"], ("bwrap", "seatbelt", "none"))


class SandboxWrapTests(unittest.TestCase):
    def test_bwrap_hides_the_store_and_binds_only_the_run_directory(self) -> None:
        box = sandbox.Sandbox(kind="bwrap", network=False, writable=[Path("/srv/tri/runs/c1")], readable=[Path("/srv/tri/envs")], hidden=[Path("/srv/tri")])
        with mock.patch.object(Path, "is_dir", return_value=True), mock.patch.object(Path, "exists", return_value=True):
            argv = box.wrap(["make", "test"], Path("/srv/tri/runs/c1/work"))
        self.assertIn("--unshare-net", argv)
        joined = " ".join(argv)
        self.assertLess(joined.index("--tmpfs /srv/tri"), joined.index("--ro-bind /srv/tri/envs"))
        self.assertLess(joined.index("--ro-bind /srv/tri/envs"), joined.index("--bind /srv/tri/runs/c1 /srv/tri/runs/c1"))
        self.assertEqual(argv[-2:], ["make", "test"])

    def test_seatbelt_profile_denies_store_then_allows_run_directory(self) -> None:
        box = sandbox.Sandbox(kind="seatbelt", network=False, writable=[Path("/tmp/run")], hidden=[Path("/tmp")])
        profile = box.wrap(["true"], Path("/tmp/run"))[2]
        self.assertIn("(deny network*)", profile)
        self.assertLess(profile.index('(deny file-read* (subpath "/tmp"))'), profile.index('(allow file-read* (subpath "/tmp/run"))'))


if __name__ == "__main__":
    unittest.main()
