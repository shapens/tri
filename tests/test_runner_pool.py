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
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tri import autoscale, cli  # noqa: E402
from tri.cli import SCHEMA_CATALOG  # noqa: E402

SECRET_VALUE = "tok_runner_canary_8c1f"


class RunnerPoolBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-runners-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_bytes(("print('v1')\n").encode())
        (repo / ".env").write_bytes(("SECRET=server-only\n").encode())
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


class RunnerPoolTests(RunnerPoolBase):
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



class RunnerSecretLeaseTests(RunnerPoolBase):
    """Remote jobs that carry secret leases: claim holder only, live claim only, redacted, audited."""

    def setUp(self) -> None:
        super().setUp()
        value_file = self.root / "secret.txt"
        value_file.write_bytes(SECRET_VALUE.encode())
        self.secret = self.server_tri("secrets", "put", "app", "API_TOKEN", "--value-file", str(value_file))["data"]["id"]
        self.grant = self.server_tri("policy", "grant", self.secret, "--principal", "actor:agent:a", "--action", "secret.use")["data"]["id"]
        self.lease = self.tri("secrets", "request-lease", self.ws, "API_TOKEN", "--env", "API_TOKEN")["data"]["id"]

    def queue(self, script: str, lease: str | None = None, ok: bool = True) -> dict:
        return self.tri("run", self.ws, "--wait", "0", "--with-lease", lease or self.lease, "--", sys.executable, "-c", script, ok=ok)

    def events(self, kind: str) -> list[dict]:
        return self.tri("events", "list", "--kind", kind, "--limit", "100")["data"]["events"]

    def test_claiming_runner_gets_the_value_and_it_is_redacted_everywhere(self) -> None:
        script = "import os; v = os.environ['API_TOKEN']; print('token=' + v); open('src/leak.txt', 'w').write(v); open('src/ok.txt', 'w').write('fine')"
        queued = self.queue(script)["data"]
        self.assertEqual(queued["secret_leases"], [{"lease": self.lease, "env": "API_TOKEN", "status": "granted", "deliveries": 0}])
        self.assertNotIn(SECRET_VALUE, json.dumps(queued))
        worked = self.runner("--max-jobs", "1")["data"]
        self.assertEqual(worked["jobs"][0]["status"], "passed", worked)
        job = self.tri("runner", "jobs", token=self.runner_token)["data"]["jobs"][0]
        self.assertEqual(job["secret_leases"][0]["status"], "completed")
        self.assertEqual(job["secret_leases"][0]["deliveries"], 1)
        check = self.check_record(job["check"])
        self.assertEqual(check["secret_leases"], [self.lease])
        self.assertEqual(check["environment_policy"]["secret_env"], ["API_TOKEN"])
        stdout = self.tri("artifacts", "read", check["logs"]["stdout"])["data"]
        self.assertNotIn(SECRET_VALUE, json.dumps(stdout))
        self.assertIn("tri-secret-redacted", json.dumps(stdout))
        self.assertNotIn(SECRET_VALUE, json.dumps(check))
        self.assertIsNone(self.read("src/leak.txt"))  # a leased value never enters source history
        self.assertEqual(self.read("src/ok.txt"), "fine")
        self.assertEqual(check["materialized_directory"]["secret_bearing_writes_skipped"], 1)
        for kind in ("run_job.secret_granted", "run_job.secret_delivered", "run_job.secrets_released"):
            self.assertTrue(self.events(kind), kind)
        self.assertNotIn(SECRET_VALUE, json.dumps(self.tri("events", "list", "--limit", "500")))
        # The runner kept nothing on disk: not in its blob cache, not in leftover run directories.
        for path in (self.root / "runner-cache").rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET_VALUE.encode(), path.read_bytes(), path)

    def check_record(self, check_id: str) -> dict:
        return self.tri("checks", "inspect", check_id)["data"]

    def test_only_the_claim_holder_can_fetch_and_only_while_the_job_runs(self) -> None:
        self.queue("pass")
        job = self.tri("runner", "claim", token=self.runner_token)["data"]["job"]
        self.assertEqual([item["lease"] for item in job["secret_leases"]], [self.lease])
        self.assertNotIn(SECRET_VALUE, json.dumps(job))
        other = self.tri("runner", "secrets", job["id"], ok=False, token=self.other_runner_token)
        self.assertEqual(other["diagnostics"][0]["code"], "policy_denied")
        agent = self.tri("runner", "secrets", job["id"], ok=False)
        self.assertEqual(agent["diagnostics"][0]["code"], "policy_denied")
        delivered = self.tri("runner", "secrets", job["id"], token=self.runner_token)["data"]
        self.assertEqual(delivered["env"], {"API_TOKEN": SECRET_VALUE})
        self.assertEqual(delivered["leases"], [self.lease])
        self.tri("runner", "complete", job["id"], "--result", json.dumps({"exit_status": 0}), token=self.runner_token)
        after = self.tri("runner", "secrets", job["id"], ok=False, token=self.runner_token)
        self.assertEqual(after["diagnostics"][0]["code"], "policy_denied")

    def test_expired_claim_lease_is_refused_and_the_next_claim_fetches_again(self) -> None:
        self.queue("pass")
        job = self.tri("runner", "claim", token=self.runner_token)["data"]["job"]
        self.tri("runner", "secrets", job["id"], token=self.runner_token)
        self.expire_claim(job["id"])
        expired = self.tri("runner", "secrets", job["id"], ok=False, token=self.runner_token)
        self.assertEqual(expired["diagnostics"][0]["code"], "policy_denied")
        self.assertTrue(self.events("run_job.secret_expired"))
        reclaimed = self.tri("runner", "claim", token=self.other_runner_token)["data"]["job"]
        self.assertEqual(reclaimed["id"], job["id"])
        self.assertEqual(self.tri("runner", "secrets", job["id"], token=self.other_runner_token)["data"]["env"], {"API_TOKEN": SECRET_VALUE})
        stale = self.tri("runner", "complete", job["id"], "--result", json.dumps({"exit_status": 0}), ok=False, token=self.runner_token)
        self.assertEqual(stale["diagnostics"][0]["code"], "policy_denied")

    def expire_claim(self, job_id: str) -> None:
        # Through the server's store, whatever its metadata backend.
        with mock.patch.dict(os.environ, {"TRI_HOME": str(self.root / "server-home")}):
            store = cli.Store(pack_on_close=False)
        try:
            with store.conn:
                store.conn.execute("UPDATE run_jobs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (job_id,))
        finally:
            store.close()

    def test_expired_secret_lease_is_refused_at_queue_and_at_delivery(self) -> None:
        short = self.tri("secrets", "request-lease", self.ws, "API_TOKEN", "--env", "API_TOKEN", "--ttl-seconds", "2")["data"]["id"]
        self.queue("pass", lease=short)
        job = self.tri("runner", "claim", token=self.runner_token)["data"]["job"]
        time.sleep(2.5)
        refused = self.tri("runner", "secrets", job["id"], ok=False, token=self.runner_token)
        self.assertEqual(refused["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(SECRET_VALUE, json.dumps(refused))
        at_queue = self.queue("pass", lease=short, ok=False)
        self.assertEqual(at_queue["diagnostics"][0]["code"], "policy_denied")

    def test_a_job_carries_only_leases_the_requester_may_use(self) -> None:
        tokens = self.root / "tokens.json"
        other_agent = self.server_tri("auth", "issue", "agent:b", "--file", str(tokens))["data"]["token"]
        borrowed = self.tri("run", self.ws, "--wait", "0", "--with-lease", self.lease, "--", sys.executable, "-c", "pass", ok=False, token=other_agent)
        self.assertEqual(borrowed["diagnostics"][0]["code"], "policy_denied")
        self.queue("pass")
        job = self.tri("runner", "claim", token=self.runner_token)["data"]["job"]
        self.server_tri("policy", "revoke", self.grant)  # policy changed after the job was queued
        revoked = self.tri("runner", "secrets", job["id"], ok=False, token=self.runner_token)
        self.assertEqual(revoked["diagnostics"][0]["code"], "policy_denied")
        self.assertNotIn(SECRET_VALUE, json.dumps(revoked))


class RunnerAutoscaleTests(RunnerPoolBase):
    """Queue metrics for external autoscalers and the local worker supervisor."""

    def test_metrics_report_queue_depth_to_runners_only(self) -> None:
        for _ in range(3):
            self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", "pass")
        metrics = self.tri("runner", "metrics", token=self.runner_token)["data"]
        self.assertEqual((metrics["queued"], metrics["claimed"], metrics["demand"]), (3, 0, 3))
        self.assertGreaterEqual(metrics["oldest_queued_age_seconds"], 0)
        self.tri("runner", "claim", token=self.runner_token)
        metrics = self.tri("runner", "metrics", token=self.runner_token)["data"]
        self.assertEqual((metrics["queued"], metrics["claimed"]), (2, 1))
        self.assertEqual(metrics["claims"], [{"runner": "service:runner1", "active": 1}])
        self.assertGreater(metrics["leases"]["soonest_expiry_seconds"], 0)
        denied = self.tri("runner", "metrics", ok=False)
        self.assertEqual(denied["diagnostics"][0]["code"], "policy_denied")
        req = request.Request(f"{self.url}/v0/runners/metrics?format=prometheus", headers={"Authorization": f"Bearer {self.runner_token}"})
        with request.urlopen(req, timeout=10) as response:
            self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))
            text = response.read().decode()
        self.assertIn("tri_run_jobs_queued 2\n", text)
        self.assertIn('tri_runner_claims{runner="service:runner1"} 1', text)
        req = request.Request(f"{self.url}/v0/runners/metrics?format=prometheus", headers={"Authorization": f"Bearer {self.agent_token}"})
        with self.assertRaises(HTTPError) as raised:
            request.urlopen(req, timeout=10)
        self.assertEqual(raised.exception.code, 403)

    def test_autoscaler_scales_up_with_the_queue_and_drains_idle_workers(self) -> None:
        for index in range(4):
            # No file writes: parallel jobs from one head would go stale on import.
            self.tri("run", self.ws, "--wait", "0", "--", sys.executable, "-c", "import time; time.sleep(1.5)")
        result = self.tri("runner", "autoscale", "--min", "0", "--max", "2", "--interval", "0.2", "--poll", "0.2", "--idle-seconds", "0.5", "--exit-when-idle", "--duration", "120", token=self.runner_token, cache="runner-cache")["data"]
        self.assertEqual(result["stopped_because"], "idle", result)
        self.assertEqual(result["peak_workers"], 2)  # never above --max
        self.assertEqual(result["workers"], 0)  # every worker drained back to --min
        self.assertGreaterEqual(result["drained"], 2)
        actions = [item["action"] for item in result["decisions"]]
        self.assertIn("start", actions)
        self.assertIn("drain", actions)
        jobs = self.tri("runner", "jobs", token=self.runner_token)["data"]["jobs"]
        self.assertEqual(sorted(job["status"] for job in jobs), ["completed"] * 4)
        self.assertEqual(len({job["runner"] for job in jobs}), 1)  # one runner identity, several processes


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    kill = terminate


class SupervisorDecisionTests(unittest.TestCase):
    def test_desired_workers_follows_demand_within_bounds(self) -> None:
        self.assertEqual(autoscale.desired_workers(0, 0, minimum=1, maximum=5), 1)
        self.assertEqual(autoscale.desired_workers(3, 1, minimum=0, maximum=5), 4)
        self.assertEqual(autoscale.desired_workers(30, 2, minimum=0, maximum=5), 5)
        self.assertEqual(autoscale.desired_workers(5, 0, minimum=0, maximum=10, jobs_per_runner=2), 3)

    def test_supervisor_never_drains_a_busy_worker(self) -> None:
        procs: list[tuple[Path, FakeProc]] = []
        queue = iter([4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])

        def spawn(control_dir: Path) -> FakeProc:
            proc = FakeProc()
            procs.append((control_dir, proc))
            # the first worker stays busy for the whole run, the rest go idle at once
            autoscale.write_status(control_dir, "busy" if len(procs) == 1 else "idle", job="job_x")
            return proc

        def metrics() -> dict:
            for control_dir, proc in procs:
                if (control_dir / autoscale.DRAIN_FILE).exists():
                    proc.returncode = 0
            return {"queued": next(queue, 0)}

        result = autoscale.supervise(metrics, minimum=0, maximum=3, idle_seconds=0.0, interval=0.01, poll=0.01, duration=0.5, spawn=spawn)
        self.assertEqual(result["peak_workers"], 3)
        busy_dir, busy_proc = procs[0]
        self.assertTrue(all(item["action"] != "drain" or item["busy"] == 1 for item in result["decisions"]))
        drained_while_running = [item for item in result["decisions"] if item["action"] == "drain"]
        self.assertTrue(drained_while_running)
        self.assertEqual(result["stopped_because"], "duration")


if __name__ == "__main__":
    unittest.main()
