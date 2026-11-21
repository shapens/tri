"""Serving benchmark: requests per second and latency for typical agent commands
through `tri serve` (one threaded Python process) versus `tri-server` (the Go
front server with a pool of persistent Python workers).

Each target gets a fresh Tri home with the same imported fixture and a token
file, so every request is authenticated, as an agent's would be. Each of C
client threads holds one keep-alive connection and its own workspace, and sends
`POST /v0/command` requests back to back:

- ``files-read``: ``files read <ws> src/fNNN.py``
- ``files-write``: ``files write <ws> out/<client>-<n>.txt --text ...``
- ``workspace-create``: ``workspace create app --from main``
- ``changeset-propose``: ``changeset create <ws>`` on workspaces prepared (untimed)
  with one edit each

It also profiles the Python per-request cost in process (``--profile``): opening
a Store, schema creation and migrations, and one read with a per-request store
versus a store kept open, which is what a worker does.

The client is Python threads in one process on the same machine as the server,
so at high request rates it competes with the server for CPU; numbers are a
same-machine comparison, not a capacity plan.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "tri.benchmark.serving.v1"
WORKLOADS = ["files-read", "files-write", "workspace-create", "changeset-propose"]


def run(argv: list[str], env: dict[str, str], *, cwd: Path = ROOT) -> dict[str, Any]:
    proc = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[:6]} failed: {proc.stdout.decode()[-400:]} {proc.stderr.decode()[-400:]}")
    return json.loads(proc.stdout)


def tri(env: dict[str, str], *argv: str) -> dict[str, Any]:
    return run([sys.executable, "-m", "tri", *argv], env)


def make_fixture(root: Path, files: int) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    for index in range(files):
        (repo / "src" / f"f{index:03d}.py").write_bytes(f"def f{index}():\n    return {index}\n".encode() * 20)
    git = ["git", "-C", str(repo), "-c", "user.email=bench@example.com", "-c", "user.name=bench", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
    return repo


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Client:
    """One keep-alive connection; reconnects when the server closes it (tri serve
    speaks HTTP/1.0 and closes after every response)."""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        self.conn: http.client.HTTPConnection | None = None

    def command(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        body = json.dumps({"argv": argv}).encode()
        for attempt in range(2):
            if self.conn is None:
                self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
            try:
                self.conn.request("POST", "/v0/command", body=body, headers=self.headers)
                resp = self.conn.getresponse()
                raw = resp.read()
                if resp.getheader("Connection", "").lower() == "close" or resp.version == 10:
                    self.conn.close()
                    self.conn = None
                return resp.status, json.loads(raw)
            except (http.client.HTTPException, ConnectionError, OSError):
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def drive(port: int, tokens: list[str], plans: list[list[list[str]]]) -> dict[str, Any]:
    """Run each client's argv list on its own thread; return throughput and latency."""
    latencies: list[float] = []
    failures: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(plans) + 1)

    def worker(index: int) -> None:
        client = Client(port, tokens[index % len(tokens)])
        mine: list[float] = []
        bad: list[str] = []
        barrier.wait()
        for argv in plans[index]:
            began = time.perf_counter()
            try:
                status, body = client.command(argv)
            except Exception as exc:  # count, do not stop the run
                bad.append(f"{argv[:2]}: {type(exc).__name__}: {exc}")
                continue
            mine.append(time.perf_counter() - began)
            if status != 200 or not body.get("ok"):
                bad.append(f"{argv[:2]}: {status} {json.dumps(body.get('diagnostics'))[:200]}")
        client.close()
        with lock:
            latencies.extend(mine)
            failures.extend(bad)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(plans))]
    for thread in threads:
        thread.start()
    barrier.wait()
    began = time.perf_counter()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - began
    count = sum(len(plan) for plan in plans)
    return {
        "requests": count,
        "ok": count - len(failures),
        "failed": len(failures),
        "failures": failures[:5],
        "wall_s": round(wall, 3),
        "rps": round((count - len(failures)) / wall, 1) if wall else 0.0,
        "p50_ms": round(percentile(latencies, 0.50) * 1000, 2),
        "p99_ms": round(percentile(latencies, 0.99) * 1000, 2),
        "mean_ms": round(statistics.fmean(latencies) * 1000, 2) if latencies else 0.0,
    }


class Target:
    def __init__(self, name: str, workdir: Path, args: argparse.Namespace) -> None:
        self.name = name
        self.workdir = workdir
        self.args = args
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("TRI_")}
        self.env.update({"TRI_HOME": str(workdir / "home"), "TRI_AUTO_REAP": "0", "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))})
        if args.journal:
            self.env["TRI_SQLITE_JOURNAL"] = args.journal
        self.proc: subprocess.Popen | None = None
        self.port = 0

    def prepare(self, repo: Path, clients: int) -> None:
        tri(self.env, "import", str(repo), "--name", "app")
        self.token_file = self.workdir / "tokens.json"
        self.tokens = []
        for index in range(clients):
            issued = tri(self.env, "auth", "issue", f"agent:bench{index}", "--file", str(self.token_file))
            self.tokens.append(issued["data"]["token"])

    def start(self) -> None:
        self.port = free_port()
        if self.name == "tri-serve":
            argv = [sys.executable, "-m", "tri", "serve", "--host", "127.0.0.1", "--port", str(self.port), "--quiet", "--auth-file", str(self.token_file)]
        else:
            argv = [self.args.native, "--host", "127.0.0.1", "--port", str(self.port), "--quiet", "--auth-file", str(self.token_file), "--workers", str(self.args.workers), "--max-workers", str(self.args.workers), "--", sys.executable, "-m", "tri", "worker", "--stdio"]
        self.log = open(self.workdir / f"{self.name}.log", "wb")
        self.proc = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                conn.request("GET", "/v0/ready")
                if conn.getresponse().status == 200:
                    conn.close()
                    return
                conn.close()
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"{self.name} did not start")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.log.close()


def plans_for(workload: str, clients: int, per_client: int, workspaces: list[str], files: int, round_id: str, proposals: list[list[str]] | None = None) -> list[list[list[str]]]:
    plans: list[list[list[str]]] = []
    for c in range(clients):
        ws = workspaces[c]
        if workload == "files-read":
            plans.append([["files", "read", ws, f"src/f{(c * 7 + n) % files:03d}.py"] for n in range(per_client)])
        elif workload == "files-write":
            plans.append([["files", "write", ws, f"out/{round_id}-{c}-{n}.txt", "--text", f"client {c} write {n}\n"] for n in range(per_client)])
        elif workload == "workspace-create":
            plans.append([["workspace", "create", "app", "--from", "main"] for _ in range(per_client)])
        elif workload == "changeset-propose":
            plans.append([["changeset", "create", prepared, "--title", f"bench {round_id} {c} {i}"] for i, prepared in enumerate((proposals or [])[c])])
    return plans


def prepare_proposals(target: Target, clients: int, per_client: int, round_id: str) -> list[list[str]]:
    """Untimed: workspaces with one edit each, one batch request per client."""
    out: list[list[str]] = []
    for c in range(clients):
        client = Client(target.port, target.tokens[c])
        commands = []
        for i in range(per_client):
            commands.append({"argv": ["workspace", "create", "app", "--from", "main"], "as": f"w{i}"})
            commands.append({"argv": ["files", "write", f"${{w{i}.data.id}}", f"proposal/{round_id}-{c}-{i}.txt", "--text", "proposed\n"]})
        conn = http.client.HTTPConnection("127.0.0.1", target.port, timeout=600)
        conn.request("POST", "/v0/batch", body=json.dumps({"commands": commands, "response": "summary", "return": [f"${{w{i}.data.id}}" for i in range(per_client)]}).encode(), headers=client.headers)
        body = json.loads(conn.getresponse().read())
        conn.close()
        if not body.get("ok"):
            raise RuntimeError(f"proposal setup failed: {json.dumps(body)[:400]}")
        out.append(body["data"]["returns"])
    return out


def bench_target(name: str, root: Path, repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    clients_max = max(args.concurrency)
    target = Target(name, root / name, args)
    target.workdir.mkdir(parents=True)
    target.prepare(repo, clients_max)
    target.start()
    results: dict[str, Any] = {}
    try:
        workspaces = []
        for c in range(clients_max):
            status, body = Client(target.port, target.tokens[c]).command(["workspace", "create", "app", "--from", "main"])
            workspaces.append(body["data"]["id"])
        # Warm up: first requests pay worker and cache startup.
        drive(target.port, target.tokens, [[["files", "read", workspaces[c], "src/f000.py"]] * 5 for c in range(clients_max)])
        for workload in WORKLOADS:
            results[workload] = {}
            for clients in args.concurrency:
                total = args.requests[workload] if workload in args.requests else args.default_requests
                per_client = max(1, total // clients)
                round_id = f"{workload}-{clients}"
                proposals = prepare_proposals(target, clients, per_client, round_id) if workload == "changeset-propose" else None
                plans = plans_for(workload, clients, per_client, workspaces, args.files, round_id, proposals)
                results[workload][str(clients)] = drive(target.port, target.tokens[:clients], plans)
                print(f"{name:10} {workload:18} c={clients:<3} {results[workload][str(clients)]}", flush=True)
    finally:
        target.stop()
    return results


def profile_python(root: Path, repo: Path) -> dict[str, Any]:
    """Per-request costs inside Python, in process."""
    import cProfile
    import io
    import pstats

    home = root / "profile-home"
    old_home = os.environ.get("TRI_HOME")
    os.environ["TRI_HOME"] = str(home)
    os.environ["TRI_AUTO_REAP"] = "0"
    try:
        began = time.perf_counter()
        from tri import cli

        import_ms = (time.perf_counter() - began) * 1000
        began = time.perf_counter()
        cli.build_parser()
        parser_ms = (time.perf_counter() - began) * 1000
        cli.execute_argv(["import", str(repo), "--name", "app"])
        ws, _ = cli.execute_argv(["workspace", "create", "app", "--from", "main"])
        ws_id = ws["data"]["id"]
        n = 300

        def per_call(fn) -> float:
            began = time.perf_counter()
            for _ in range(n):
                fn()
            return round((time.perf_counter() - began) * 1000 / n, 3)

        def open_close() -> None:
            cli.Store(pack_on_close=False).close()

        shared = cli.Store(pack_on_close=False)
        out = {
            "python_import_ms": round(import_ms, 1),
            "parser_build_ms": round(parser_ms, 1),
            "store_open_close_ms": per_call(open_close),
            "init_db_ms": per_call(shared.init_db),
            "migrate_db_ms": per_call(shared.migrate_db),
            "files_read_per_request_store_ms": per_call(lambda: cli.execute_argv(["files", "read", ws_id, "src/f001.py"], pack_on_close=False, forward_remote=False)),
            "files_read_shared_store_ms": per_call(lambda: cli.execute_argv(["files", "read", ws_id, "src/f001.py"], store=shared)),
        }

        def shared_cleared() -> None:
            cli.execute_argv(["files", "read", ws_id, "src/f001.py"], store=shared)
            shared._blob_cache.clear()
            shared._tree_cache.clear()

        out["files_read_shared_store_caches_cleared_ms"] = per_call(shared_cleared)
        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(200):
            cli.execute_argv(["files", "read", ws_id, "src/f001.py"], pack_on_close=False, forward_remote=False)
        profiler.disable()
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(12)
        out["cprofile_files_read_per_request_store"] = [line for line in stream.getvalue().splitlines() if line.strip()][:30]
        shared.close()
        return out
    finally:
        if old_home is None:
            os.environ.pop("TRI_HOME", None)
        else:
            os.environ["TRI_HOME"] = old_home


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Serving benchmark",
        "",
        f"- Created: `{payload['created_at']}`",
        f"- Platform: `{payload['platform']}`, {payload['cpus']} CPUs, Python {payload['python']}",
        f"- Fixture: `{payload['args']['files']}` files; every request authenticated with a bearer token over loopback; SQLite journal `{payload['args'].get('journal', 'DELETE')}`",
        f"- tri-server workers: `{payload['args']['workers']}` (fixed pool); `tri serve` is one process with a thread per request",
        "- Client: Python threads in one process on the same machine, one keep-alive connection each (tri serve closes after every response)",
        "",
        "| Workload | Clients | tri serve rps | tri-server rps | Speedup | tri serve p50 / p99 ms | tri-server p50 / p99 ms | Failed (serve / server) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    serve, native = payload["targets"].get("tri-serve", {}), payload["targets"].get("tri-server", {})
    for workload in WORKLOADS:
        for clients in payload["args"]["concurrency"]:
            a = serve.get(workload, {}).get(str(clients))
            b = native.get(workload, {}).get(str(clients))
            if not a or not b:
                continue
            speedup = f"{b['rps'] / a['rps']:.1f}x" if a["rps"] else "-"
            lines.append(f"| {workload} | {clients} | {a['rps']} | {b['rps']} | {speedup} | {a['p50_ms']} / {a['p99_ms']} | {b['p50_ms']} / {b['p99_ms']} | {a['failed']} / {b['failed']} |")
    profile = payload.get("profile")
    if profile:
        lines += [
            "",
            "## Python per-request cost (in process)",
            "",
            "| Measurement | ms |",
            "| --- | ---: |",
        ]
        for key, value in profile.items():
            if not isinstance(value, list):
                lines.append(f"| `{key}` | {value} |")
    lines.append("")
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tri-bench-serving", description=__doc__.split("\n\n")[0])
    parser.add_argument("--native", default=str(ROOT / "native" / "tri-server" / ("tri-server.exe" if os.name == "nt" else "tri-server")))
    parser.add_argument("--targets", default="tri-serve,tri-server")
    parser.add_argument("--concurrency", default="1,8,32")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--requests", default="files-read=2000,files-write=800,workspace-create=400,changeset-propose=160", help="total requests per workload and concurrency")
    parser.add_argument("--profile", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--journal", help="TRI_SQLITE_JOURNAL for both servers (default: the store's default, DELETE)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    args.concurrency = [int(item) for item in args.concurrency.split(",") if item]
    args.requests = {key: int(value) for key, value in (item.split("=") for item in args.requests.split(",") if item)}
    args.default_requests = 400
    targets = [item for item in args.targets.split(",") if item]
    if "tri-server" in targets and not Path(args.native).exists():
        raise SystemExit(f"tri-server binary not found at {args.native}; build it with `go build` in native/tri-server")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output = args.output or ROOT / "benchmark-results" / f"serving-{stamp.replace(':', '')}"
    root = Path(tempfile.mkdtemp(prefix="tri-bench-serving-"))
    try:
        repo = make_fixture(root, args.files)
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "created_at": stamp,
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
            "args": {"targets": targets, "concurrency": args.concurrency, "workers": args.workers, "files": args.files, "requests": args.requests, "journal": args.journal or "DELETE"},
            "targets": {},
        }
        for name in targets:
            payload["targets"][name] = bench_target(name, root, repo, args)
        if args.profile:
            payload["profile"] = profile_python(root, repo)
        output.mkdir(parents=True, exist_ok=True)
        (output / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
        write_summary(output / "summary.md", payload)
        print((output / "summary.md").read_text())
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
