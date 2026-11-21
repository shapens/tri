"""`tri worker --stdio`: the framed protocol tri-server speaks to its Python workers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkerProcess:
    def __init__(self, env: dict[str, str], *args: str) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "tri", "worker", "--stdio", *args], cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def read(self) -> tuple[dict, bytes]:
        header = json.loads(self.proc.stdout.readline())
        return header, self.proc.stdout.read(header["length"])

    def send(self, header: dict, body: bytes = b"") -> None:
        self.proc.stdin.write(json.dumps({**header, "length": len(body)}).encode() + b"\n" + body)
        self.proc.stdin.flush()

    def request(self, method: str, path: str, payload: object | None = None, *, actor: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        self.next_id += 1
        body = json.dumps(payload).encode() if payload is not None else b""
        all_headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.send({"type": "request", "id": self.next_id, "method": method, "path": path, "headers": all_headers, "actor": actor}, body)
        header, raw = self.read()
        assert header["type"] == "response" and header["id"] == self.next_id, header
        return header["status"], json.loads(raw)

    def close(self) -> int:
        self.proc.stdin.close()
        code = self.proc.wait(timeout=30)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tri-worker-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("TRI_")}
        self.env.update({"TRI_HOME": str(root / "home"), "TRI_AUTO_REAP": "0", "PYTHONPATH": str(ROOT)})
        repo = root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "a.txt").write_bytes(b"alpha\n")
        git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "core.autocrlf=false"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        out = subprocess.run([sys.executable, "-m", "tri", "import", str(repo), "--name", "app", "--target", "main"], cwd=ROOT, env=self.env, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stdout[-400:])
        self.server_file = root / "server.txt"
        self.server_file.write_bytes(b"server\n")

    def start(self, *args: str) -> WorkerProcess:
        worker = WorkerProcess(self.env, *args)
        self.addCleanup(lambda: worker.proc.poll() is None and worker.proc.kill())
        return worker

    def test_hello_requests_maintenance_and_clean_exit(self) -> None:
        worker = self.start("--port", "9")
        hello, _ = worker.read()
        self.assertEqual((hello["type"], hello["protocol"], hello["auth"]), ("hello", 1, "none"))
        self.assertEqual(hello["startup"]["schema"], "tri.api.server")
        self.assertIn("workspace", hello["commands"])
        status, body = worker.request("GET", "/v0/health")
        self.assertEqual((status, body["schema"], body["data"]["home"]), (200, "tri.api.health", hello["home"]))
        status, body = worker.request("POST", "/v0/command", {"argv": ["workspace", "create", "app", "--from", "main"]})
        self.assertTrue(body["ok"], body)
        ws = body["data"]["id"]
        # The worker's stdin is the protocol: a command reading stdin must not eat frames.
        status, body = worker.request("POST", "/v0/command", {"argv": ["batch", "-"]})
        self.assertEqual(body["diagnostics"][0]["code"], "invalid_command")
        status, body = worker.request("POST", "/v0/command", {"argv": ["files", "read", ws, "src/a.txt"]})
        self.assertEqual(body["data"]["text"], "alpha\n")
        status, body = worker.request("POST", "/v0/batch", {"commands": [{"argv": ["files", "write", ws, "b.txt", "--text", "beta\n"]}, {"argv": ["files", "read", ws, "b.txt"], "as": "r"}], "return": "${r.data.text}"})
        self.assertEqual(body["data"]["returns"], "beta\n")
        status, body = worker.request("GET", "/v0/nowhere")
        self.assertEqual((status, body["diagnostics"][0]["code"]), (404, "path_not_found"))
        status, body = worker.request("POST", "/v0/command", {"argv": ["worker", "--stdio"]})
        self.assertEqual(body["diagnostics"][0]["code"], "invalid_command")
        worker.send({"type": "maintain", "id": 99})
        header, _ = worker.read()
        self.assertEqual((header["type"], header["id"], header["ok"]), ("maintained", 99, True))
        self.assertEqual(worker.close(), 0)

    def test_pinned_actor_and_server_paths(self) -> None:
        worker = self.start("--auth-file", str(Path(self.tmp.name) / "tokens.json"))
        hello, _ = worker.read()
        self.assertEqual(hello["auth"], "bearer")
        status, body = worker.request("GET", "/v0/health", actor=None)
        self.assertIsNone(body["data"]["home"])
        status, body = worker.request("POST", "/v0/command", {"argv": ["workspace", "list"]}, actor="agent:a")
        self.assertEqual(body["actor"], "agent:a")
        status, body = worker.request("GET", "/v0/projects", actor="agent:a", headers={"X-Tri-Actor": "agent:b"})
        self.assertEqual((status, body["diagnostics"][0]["code"]), (403, "actor_mismatch"))
        status, body = worker.request("POST", "/v0/command", {"argv": ["workspace", "list"], "actor": "agent:b"}, actor="agent:a")
        self.assertEqual(body["diagnostics"][0]["code"], "actor_mismatch")
        status, body = worker.request("POST", "/v0/command", {"argv": ["workspace", "create", "app", "--from", "main"]}, actor="agent:a")
        ws = body["data"]["id"]
        status, body = worker.request("POST", "/v0/command", {"argv": ["files", "write", ws, "x", "--body", str(self.server_file)]}, actor="agent:a")
        self.assertEqual((body["diagnostics"][0]["code"], body["diagnostics"][0]["reason"]), ("policy_denied", "server_path"))
        status, body = worker.request("POST", "/v0/command", {"argv": ["files", "write", ws, "x", "--body", str(self.server_file)]}, actor="actor_local")
        self.assertTrue(body["ok"], body)
        self.assertEqual(worker.close(), 0)

    def test_policy_refusal_is_a_fatal_frame(self) -> None:
        worker = self.start("--host", "0.0.0.0")
        header, body = worker.read()
        self.assertEqual(header["type"], "fatal")
        envelope = json.loads(body)
        self.assertEqual(envelope["diagnostics"][0]["code"], "unauthenticated")
        self.assertEqual(worker.proc.wait(timeout=30), 1)
        worker.proc.stdin.close()
        worker.proc.stdout.close()
        worker.proc.stderr.close()

    def test_worker_is_refused_through_the_api_and_runs_locally(self) -> None:
        from tri import cli
        from tri.remote import LOCAL_ONLY_COMMANDS

        self.assertIn("worker", cli.API_BLOCKED_COMMANDS)
        self.assertIn("worker", LOCAL_ONLY_COMMANDS)


if __name__ == "__main__":
    unittest.main()
