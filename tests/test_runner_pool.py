"""Remote runner pool: runs leave the store machine and execute on claimed runners."""

from __future__ import annotations

import json
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
from urllib import request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import cli  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402


class RunnerPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-runners-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("print('v1')\n")
        (repo / ".env").write_text("SECRET=server-only\n")
        self.server_env = {**os.environ, "TRI_HOME": str(self.root / "server-home"), "TRI_AUTO_REAP": "0"}
        for key in ("TRI_REMOTE", "TRI_ACTOR", "TRI_TOKEN", "TRI_TOKEN_FILE"):
            self.server_env.pop(key, None)
        self.server_tri("import", str(repo), "--name", "app", "--target", "main")
        tokens = self.root / "tokens.json"
        self.agent_token = self.server_tri("auth", "issue", "agent:a", "--file", str(tokens))["data"]["token"]
        self.runner_token = self.server_tri("auth", "issue", "service:runner1", "--file", str(tokens))["data"]["token"]
        self.other_runner_token = self.server_tri("auth", "issue", "service:runner2", "--file", str(tokens))["data"]["token"]
        port = self.start_server(tokens)
        self.url = f"http://127.0.0.1:{port}"
        patcher = mock.patch.dict(os.environ, {"TRI_REMOTE": self.url, "TRI_TOKEN": self.agent_token, "TRI_HOME": str(self.root / "client-home"), "TRI_CACHE_DIR": str(self.root / "agent-cache")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TRI_ACTOR", None)
        self.ws = self.tri("workspace", "create", "app", "--from", "main")["data"]["id"]

    def server_tri(self, *args: str) -> dict:
        result = subprocess.run([sys.executable, "-m", "tri", *args], cwd=ROOT, env=self.server_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = json.loads(result.stdout)
        self.assertTrue(output["ok"], output)
        return output

    def start_server(self, tokens: Path) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, "-m", "tri", "serve", "--port", str(port), "--quiet", "--auth-file", str(tokens), "--run-executor", "remote"], cwd=ROOT, env=self.server_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

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

    def tri(self, *argv: str, ok: bool = True, token: str | None = None, cache: str = "agent-cache") -> dict:
        env = {"TRI_TOKEN": token or self.agent_token, "TRI_CACHE_DIR": str(self.root / cache)}
        with mock.patch.dict(os.environ, env):
            output, _ = cli.execute_argv(list(argv))
        self.assertEqual(output["ok"], ok, output)
        for diagnostic in output["diagnostics"]:
            self.assertIn(diagnostic["code"], SCHEMA_CATALOG["tri.diagnostic"]["codes"])
        schema = SCHEMA_CATALOG.get(output["schema"])
        if schema and output["ok"] and isinstance(output["data"], dict):
            missing = [key for key in schema.get("required", []) if key not in output["data"]]
            self.assertEqual(missing, [], f"{output['schema']} missing {missing}")
        return output

    def runner(self, *extra: str, token: str | None = None) -> dict:
        return self.tri("runner", "work", "--once", "--interval", "0.2", *extra, token=token or self.runner_token, cache="runner-cache")

    def read(self, path: str) -> str | None:
        output, _ = cli.execute_argv(["files", "read", self.ws, path])
        return output["data"].get("text") if output["ok"] else None

    def test_run_queues_on_the_server_and_a_runner_executes_it(self) -> None:
        script = "import os, pathlib; pathlib.Path('src/out.txt').write_text(os.getcwd() + '|' + os.environ.get('TRI_TOKEN', '') + '|' + str(pathlib.Path('.env').exists()))"
        queued = self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", script)
        self.assertEqual(queued["schema"], "tri.run_job.inspect")
        self.assertEqual(queued["data"]["status"], "queued")
        worked = self.runner("--max-jobs", "1")["data"]
        self.assertEqual(worked["count"], 1)
        self.assertEqual(worked["jobs"][0]["status"], "passed")
        cwd, token, env_present = (self.read("src/out.txt") or "||").split("|")
        self.assertIn("tri-runner-", cwd)  # ran in the runner's directory, not the server's
        self.assertEqual(token, "")  # the runner's token never reaches the job
        self.assertEqual(env_present, "False")  # secret-like files are not sent to runners
        jobs = self.tri("runner", "jobs", token=self.runner_token)["data"]["jobs"]
        self.assertEqual(jobs[0]["status"], "completed")

    def test_waiting_run_returns_the_check_when_a_runner_finishes(self) -> None:
        results: dict[str, dict] = {}

        def agent() -> None:
            with mock.patch.dict(os.environ, {"TRI_TOKEN": self.agent_token}):
                results["run"], _ = cli.execute_argv(["run", self.ws, "--wait", "30", "--", sys.executable, "-c", "raise SystemExit(3)"])

        thread = threading.Thread(target=agent)
        thread.start()
        time.sleep(0.5)
        self.runner("--max-jobs", "1")
        thread.join(timeout=40)
        run = results["run"]
        self.assertEqual(run["schema"], "tri.check.inspect")
        self.assertEqual(run["data"]["status"], "failed")
        self.assertEqual(run["data"]["exit_status"], 3)
        self.assertTrue(run["data"]["runner"].startswith("remote:service:runner1"))

    def test_agents_cannot_act_as_runners(self) -> None:
        self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", "pass")
        denied = self.tri("runner", "claim", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")

    def test_runners_only_see_and_finish_their_own_jobs(self) -> None:
        self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", "pass")
        job = self.tri("runner", "claim", token=self.runner_token)["data"]["job"]
        self.assertNotIn(".env", [item["path"] for item in job["files"]])
        outside = self.tri("runner", "fetch", job["id"], "--path", ".env", ok=False, token=self.runner_token)
        self.assertEqual(outside["diagnostics"][0]["code"], "policy_denied")
        foreign = self.tri("runner", "complete", job["id"], "--result", json.dumps({"exit_status": 0}), ok=False, token=self.other_runner_token)
        self.assertEqual(foreign["diagnostics"][0]["code"], "policy_denied")
        smuggle = {"exit_status": 0, "written": [{"path": ".env", "content": "U0VDUkVUPW93bmVkCg=="}]}
        self.tri("runner", "complete", job["id"], "--result", json.dumps(smuggle), token=self.runner_token)
        self.assertIsNone(self.read(".env"))  # hidden from the requester: never overwritten

    def test_remote_run_changes_are_not_imported_over_a_moved_workspace(self) -> None:
        self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", "open('src/app.py','w').write('runner\\n')")
        job = self.tri("runner", "claim", token=self.runner_token)["data"]["job"]
        self.tri("files", "write", self.ws, "src/app.py", "--text", "agent moved on\n")
        result = {"exit_status": 0, "written": [{"path": "src/app.py", "content": "cnVubmVyCg=="}], "deleted": []}
        stale = self.tri("runner", "complete", job["id"], "--result", json.dumps(result), ok=False, token=self.runner_token)
        self.assertEqual(stale["diagnostics"][0]["code"], "stale_workspace_head")
        self.assertEqual(self.read("src/app.py"), "agent moved on\n")

    def test_runner_cache_serves_repeat_jobs(self) -> None:
        for _ in range(2):
            self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", "pass")
        self.runner("--max-jobs", "2")
        cached = list((self.root / "runner-cache" / "blobs").rglob("*"))
        self.assertTrue(any(path.is_file() for path in cached))


if __name__ == "__main__":
    unittest.main()
