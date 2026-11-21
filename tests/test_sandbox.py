"""Sandboxed runs: no Tri home or credential reads, no stray writes, no network, limits."""

from __future__ import annotations

import os
import socket
import subprocess
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

PROBE = sandbox.probe()
ISOLATED = PROBE["isolated"]
# A container runs the image's interpreter, not this host's.
SANDBOX_PYTHON = "python3" if PROBE["kind"] == "container" else sys.executable


def container_probe() -> dict:
    with mock.patch.dict(os.environ, {sandbox.BACKEND_ENV: "container"}):
        return sandbox.probe()


CONTAINER = container_probe()

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
try:
    results["list_home"] = "leaked" if "canary.txt" in os.listdir(sys.argv[2]) else "hidden"
except Exception:
    results["list_home"] = "hidden"
attempt("write_outside", lambda: open(sys.argv[3], "w").write("x"))
attempt("network", lambda: socket.create_connection(("127.0.0.1", int(sys.argv[4])), timeout=2).close())
attempt("external_network", lambda: socket.create_connection(("1.1.1.1", 53), timeout=3).close())
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
        (repo / "app.txt").write_bytes(("app\n").encode())
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

    def probe(self, *flags: str, python: str = sys.executable) -> tuple[dict, dict[str, str]]:
        port = self.listener()
        outside = self.root / "outside.txt"
        canary = self.home / "canary.txt"  # a store file, whatever the metadata backend
        canary.write_bytes(b"store-only\n")
        check = self.tri("run", self.ws, *flags, "--", python, "-c", PROBE_SCRIPT, str(canary), str(self.home), str(outside), str(port))["data"]
        return check, dict(eval(self.read("probe.txt") or "[]"))  # noqa: S307 - written by the probe above

    def test_runs_are_unsandboxed_by_default_on_a_workstation(self) -> None:
        check, results = self.probe()
        self.assertEqual(check["environment_policy"]["sandbox"]["kind"], "off")
        self.assertEqual(results["read_store"], "allowed")
        self.assertEqual(results["env_secret"], "visible")

    @unittest.skipUnless(ISOLATED, "this host has no sandbox backend")
    def test_sandboxed_run_cannot_read_the_store_write_outside_or_reach_the_network(self) -> None:
        check, results = self.probe("--sandbox", python=SANDBOX_PYTHON)
        policy = check["environment_policy"]["sandbox"]
        self.assertTrue(policy["isolated"], policy)
        self.assertEqual(policy["backend"], PROBE["kind"])
        self.assertEqual(results["read_store"], "blocked")
        self.assertEqual(results["list_home"], "hidden")  # bwrap shows an empty tmpfs, seatbelt and winjob refuse
        self.assertEqual(results["write_outside"], "blocked")
        if policy["network_enforced"]:
            self.assertEqual(results["network"], "blocked")
            self.assertEqual(results["external_network"], "blocked")
        else:
            self.assertEqual(PROBE["kind"], "winjob")  # documented: Windows cannot block the network without admin rights
        self.assertEqual(results["env_secret"], "hidden")
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual(check["status"], "passed")  # writes inside the run directory still import

    @unittest.skipUnless(ISOLATED, "this host has no sandbox backend")
    def test_server_mode_sandboxes_every_run(self) -> None:
        with mock.patch.dict(os.environ, {sandbox.MODE_ENV: "required"}):
            check, results = self.probe(python=SANDBOX_PYTHON)
        self.assertTrue(check["environment_policy"]["sandbox"]["isolated"])
        self.assertEqual(results["read_store"], "blocked")

    @unittest.skipIf(os.name == "nt" and not ISOLATED, "without a job object, Windows kills only the direct child")
    def test_timeout_kills_the_process_group(self) -> None:
        started = time.time()
        with mock.patch.dict(os.environ, {sandbox.MODE_ENV: "auto"}):
            check = self.tri("run", self.ws, "--timeout", "1", "--", SANDBOX_PYTHON, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)")["data"]
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
        reported = self.tri("doctor")["data"]["sandbox"]
        self.assertIn(reported["kind"], ("bwrap", "seatbelt", "winjob", "container", "none"))
        self.assertIn("backend", reported)
        self.assertIn("network_enforced", reported)

    def test_unknown_backend_falls_back_to_the_platform_default(self) -> None:
        self.assertEqual(sandbox.resolve_backend("firecracker-native"), "auto")
        self.assertEqual(sandbox.resolve_backend("Container"), "container")


@unittest.skipIf(os.name == "nt", "the expected argv uses POSIX bind paths; Windows hosts translate them")
class ContainerWrapTests(unittest.TestCase):
    """The engine command line, checked without an engine."""

    def box(self, **overrides) -> sandbox.Sandbox:
        fields = {
            "kind": "container",
            "network": False,
            "writable": [Path("/srv/tri/runs/c1")],
            "readable": [Path("/srv/tri/envs")],
            "hidden": [Path("/srv/tri")],
            "limits": sandbox.Limits(memory_mb=256, cpus=1.5, cpu_seconds=30, file_size_mb=8, pids=64),
            "container": sandbox.Container(engine="docker", runtime="runsc", image="example/img:1", user="1000:1000"),
        }
        fields.update(overrides)
        return sandbox.Sandbox(**fields)

    def test_container_is_locked_down_and_binds_only_the_run_directory_writable(self) -> None:
        with mock.patch.object(Path, "exists", return_value=True):
            argv, engine_env = self.box()._container(["make", "test"], Path("/srv/tri/runs/c1/work"), name="tri-run-x", env={"HOME": "/srv/tri/runs/c1/home", "PATH": "/opt/host/bin", "API_TOKEN": "s3cret"})
        joined = " ".join(argv)
        for expected in ("--rm", "--name tri-run-x", "--runtime runsc", "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 1000:1000", "--pids-limit 64", "--memory 256m", "--cpus 1.5", "--ulimit cpu=30:35", "--workdir /srv/tri/runs/c1/work"):
            self.assertIn(expected, joined)
        mounts = [argv[index + 1] for index, part in enumerate(argv) if part == "--mount"]
        self.assertEqual(mounts, ["type=bind,source=/srv/tri/envs,target=/srv/tri/envs,readonly", "type=bind,source=/srv/tri/runs/c1,target=/srv/tri/runs/c1"])
        self.assertNotIn("/srv/tri,", joined)  # the store itself is never mounted
        self.assertIn(f"PATH={sandbox.OCI_PATH}", argv)  # the host PATH means nothing in the image
        self.assertNotIn("s3cret", joined)  # secret values travel in the engine's environment, never argv
        self.assertIn("API_TOKEN", argv)
        self.assertEqual(engine_env["API_TOKEN"], "s3cret")
        self.assertEqual(argv[argv.index("--entrypoint") + 1 :], ["make", "example/img:1", "test"])

    def test_network_allowed_uses_the_default_bridge_and_runc_omits_runtime(self) -> None:
        box = self.box(network=True, container=sandbox.Container(engine="podman", runtime=None, image="img", user="1:1"))
        argv, _ = box._container(["true"], Path("/srv/tri/runs/c1"), name="n", env={})
        self.assertEqual(argv[0], "podman")
        self.assertIn("bridge", argv)
        self.assertNotIn("--runtime", argv)
        self.assertEqual(box.describe()["runtime"], "runc")
        self.assertFalse(box.describe()["container"]["microvm"])
        self.assertTrue(self.box(container=sandbox.Container(engine="docker", runtime="kata-fc", image="img", user="1:1")).describe()["container"]["microvm"])


@unittest.skipUnless(CONTAINER["isolated"], f"no container engine: {CONTAINER.get('reason')}")
class ContainerSandboxTests(unittest.TestCase):
    """Live runs under the container backend (docker or podman; runc, runsc, or kata-fc per TRI_RUN_OCI_RUNTIME)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-oci-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "app.txt").write_bytes(b"app\n")
        self.home = self.root / "tri-home"
        env = {"TRI_HOME": str(self.home), "TRI_AUTO_REAP": "0", "TRI_TEST_CANARY_SECRET": "canary-value", sandbox.BACKEND_ENV: "container", sandbox.MODE_ENV: "required"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        cli.execute_argv(["import", str(repo), "--name", "app", "--target", "main"])
        self.ws = cli.execute_argv(["workspace", "create", "app", "--from", "main"])[0]["data"]["id"]

    def run_check(self, script: str, *flags: str) -> dict:
        output, _ = cli.execute_argv(["run", self.ws, *flags, "--", "python3", "-c", script])
        self.assertTrue(output["ok"], output)
        return output["data"]

    def read(self, path: str) -> str | None:
        output, _ = cli.execute_argv(["files", "read", self.ws, path])
        return output["data"].get("text") if output["ok"] else None

    def test_container_runs_as_non_root_on_a_read_only_root_without_network(self) -> None:
        script = (
            "import os, socket\n"
            "r = {'uid': os.getuid()}\n"
            "try:\n open('/etc/tri-stray', 'w'); r['root_write'] = 'allowed'\n"
            "except OSError:\n r['root_write'] = 'blocked'\n"
            "try:\n socket.create_connection(('1.1.1.1', 53), timeout=3); r['net'] = 'allowed'\n"
            "except OSError:\n r['net'] = 'blocked'\n"
            "open('out.txt', 'w').write(repr(sorted(r.items())))\n"
        )
        check = self.run_check(script)
        self.assertEqual(check["status"], "passed", check)
        policy = check["environment_policy"]["sandbox"]
        self.assertEqual(policy["kind"], "container")
        self.assertEqual(policy["runtime"], CONTAINER["runtime"])
        results = dict(eval(self.read("out.txt") or "[]"))  # noqa: S307 - written by the script above
        self.assertNotEqual(results["uid"], 0)
        self.assertEqual(results["root_write"], "blocked")
        self.assertEqual(results["net"], "blocked")

    def test_timeout_kills_and_removes_the_container(self) -> None:
        started = time.time()
        check = self.run_check("import time; time.sleep(120)", "--timeout", "3")
        self.assertLess(time.time() - started, 90)
        self.assertEqual(check["status"], "failed")
        self.assertTrue(check["environment_policy"]["sandbox"]["timed_out"])
        engine = CONTAINER["engine"]
        left = subprocess.run([engine, "ps", "-aq", "--filter", "label=tri.sandbox=run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(left.stdout.strip(), "", "a timed-out run container was left behind")

    def test_memory_limit_is_enforced(self) -> None:
        with mock.patch.dict(os.environ, {"TRI_RUN_MEMORY_MB": "64"}):
            check = self.run_check("x = bytearray(512 * 1024 * 1024); print(len(x))")
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["environment_policy"]["sandbox"]["limits"]["memory_mb"], 64)


@unittest.skipUnless(PROBE["kind"] == "winjob", f"the Windows job-object backend is not active here: {PROBE.get('reason') or PROBE['kind']}")
class WindowsJobSandboxTests(unittest.TestCase):
    """Job Object limits and low integrity, exercised directly (Windows CI)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-winjob-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def run_box(self, script: str, **limits) -> sandbox.Completed:
        box = sandbox.build("required", run_dir=self.run_dir, hidden=[self.root / "store"], limits=sandbox.Limits(timeout_seconds=60, **limits))
        self.assertEqual(box.kind, "winjob")
        self.assertFalse(box.describe()["network_enforced"])  # documented: no network blocking without admin rights
        return sandbox.run(box, [sys.executable, "-c", script], cwd=self.run_dir, env=sandbox.scrubbed_env(self.run_dir))

    def test_child_runs_at_low_integrity_and_writes_only_the_run_directory(self) -> None:
        (self.root / "store").mkdir()
        (self.root / "store" / "blob").write_bytes(b"store")
        script = (
            "import sys\n"
            "out = []\n"
            "for path, mode in ((r'%s', 'rb'), (r'%s', 'w'), ('inside.txt', 'w')):\n"
            "    try:\n"
            "        open(path, mode).close(); out.append('ok')\n"
            "    except OSError:\n"
            "        out.append('denied')\n"
            "print(','.join(out))\n"
        ) % (self.root / "store" / "blob", self.root / "outside.txt")
        completed = self.run_box(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.decode().strip(), "denied,denied,ok")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_process_limit_stops_fork_bombs(self) -> None:
        script = (
            "import subprocess, sys\n"
            "started = 0\n"
            "procs = []\n"
            "for _ in range(8):\n"
            "    try:\n"
            "        procs.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])); started += 1\n"
            "    except OSError:\n"
            "        pass\n"
            "print(started)\n"
            "[p.kill() for p in procs]\n"
        )
        completed = self.run_box(script, pids=4)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(int(completed.stdout.decode().strip() or "99"), 8)

    def test_memory_limit_is_enforced(self) -> None:
        completed = self.run_box("x = bytearray(512 * 1024 * 1024); print(len(x))", memory_mb=128)
        self.assertNotEqual(completed.returncode, 0)


@unittest.skipIf(os.name == "nt", "bubblewrap and seatbelt profiles use POSIX paths")
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
