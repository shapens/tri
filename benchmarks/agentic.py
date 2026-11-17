from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import URLError


SCHEMA = "tri.benchmark.agentic.v1"
ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "benchmarks" / "workflow" / "scenarios"
DEFAULT_SCENARIO = "default"
PHASES = ("setup", "workflow", "export", "verify")
COUNT_METRICS = {
    "actions",
    "agent_io_bytes",
    "commands",
    "embedded_calls",
    "external_commands",
    "http_requests",
    "requests",
    "response_bytes",
    "stderr_bytes",
    "stdin_bytes",
    "stdout_bytes",
    "store_bytes",
    "transport_bytes",
}

SETUP_STEP_PREFIXES = (
    "fixture.",
    "git.init",
    "git.config.",
    "git.add.initial",
    "git.commit.initial",
    "svnadmin.create",
    "svn.import.",
    "svn.mkdir.",
    "gitbutler.version",
    "gitbutler.setup",
    "gitbutler.status.initial",
    "entire.version",
    "entire.enable",
    "entire.status.initial",
    "git.add.entire.config",
    "git.commit.entire.config",
    "tri.batch.plan",
    "tri.serve.start",
    "tri.serve.health",
    "git.batch.plan",
)
EXPORT_STEP_PREFIXES = (
    "git.compare.",
    "git.format_patch",
    "git.patch.write",
    "svn.compare.",
    "svn.diff.export",
    "svn.patch.write",
    "gitbutler.compare.",
    "gitbutler.branch.list",
    "gitbutler.show.",
    "gitbutler.oplog.",
    "gitbutler.format_patch",
    "gitbutler.patch.write",
    "entire.compare.",
    "entire.session.list",
    "entire.status.final",
    "entire.doctor",
    "entire.format_patch",
    "entire.patch.write",
)


class BenchFailure(Exception):
    pass


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def directories_size(paths: list[Path]) -> int:
    return sum(directory_size(path) for path in paths)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(
    steps: list[dict[str, Any]],
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    check: bool = True,
    phase: str | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    step = {
        "name": name,
        "kind": "command",
        "argv": argv,
        "cwd": str(cwd),
        "returncode": result.returncode,
        "ok": result.returncode == 0,
        "wall_ms": wall_ms,
        "stdin_bytes": len(stdin.encode()) if stdin is not None else 0,
        "stdout_bytes": len(result.stdout.encode()),
        "stderr_bytes": len(result.stderr.encode()),
    }
    if phase is not None:
        step["phase"] = phase
    if result.returncode != 0:
        step["stderr_tail"] = result.stderr[-2000:]
    steps.append(step)
    if check and result.returncode != 0:
        raise BenchFailure(f"{name} failed: {' '.join(argv)}\n{result.stderr}")
    return result


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_process(
    steps: list[dict[str, Any]],
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    phase: str | None = None,
) -> subprocess.Popen[str]:
    started = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    steps.append(
        {
            "name": name,
            "kind": "command",
            "argv": argv,
            "cwd": str(cwd),
            "returncode": None,
            "ok": True,
            "wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "stdin_bytes": 0,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            **({"phase": phase} if phase is not None else {}),
        }
    )
    return proc


def stop_process(steps: list[dict[str, Any]], name: str, proc: subprocess.Popen[str]) -> None:
    started = time.perf_counter()
    if proc.poll() is None:
        proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    steps.append(
        {
            "name": name,
            "kind": "action",
            "ok": proc.returncode in (0, -15),
            "returncode": proc.returncode,
            "wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "stdout_bytes": len((stdout or "").encode()),
            "stderr_bytes": len((stderr or "").encode()),
            "stderr_tail": (stderr or "")[-2000:],
            "phase": "setup",
        }
    )


def http_json(
    steps: list[dict[str, Any]],
    name: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    actor: str | None = None,
    check: bool = True,
    phase: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if actor:
        headers["X-Tri-Actor"] = actor
    req = request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    status = 0
    raw = b""
    error = ""
    try:
        with request.urlopen(req, timeout=10) as response:
            status = int(response.status)
            raw = response.read()
    except Exception as exc:
        error = str(exc)
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    data: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw.decode())
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError as exc:
            error = f"invalid JSON response: {exc}"
    ok = status == 200 and not error and (not data or data.get("ok", False))
    step = {
        "name": name,
        "kind": "http",
        "method": req.get_method(),
        "url": url,
        "returncode": status,
        "ok": ok,
        "wall_ms": wall_ms,
        "stdin_bytes": len(body or b""),
        "stdout_bytes": len(raw),
        "stderr_bytes": len(error.encode()),
    }
    if phase is not None:
        step["phase"] = phase
    if data.get("schema"):
        step["schema"] = data.get("schema")
        step["tri_cost"] = data.get("cost")
        step["tri_events"] = len(data.get("events", []))
    if error:
        step["stderr_tail"] = error[-2000:]
    steps.append(step)
    if check and not ok:
        diagnostics = data.get("diagnostics") if data else None
        raise BenchFailure(f"{name} failed over HTTP: status={status} error={error} diagnostics={diagnostics}")
    return data


def wait_for_http_health(steps: list[dict[str, Any]], name: str, url: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.time() + 10
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=5)
            raise BenchFailure(f"{name} exited before becoming healthy\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
        try:
            http_json(steps, name, url, check=True, phase="setup")
            return
        except (BenchFailure, URLError) as exc:
            last_error = str(exc)
            time.sleep(0.05)
    raise BenchFailure(f"{name} did not become healthy: {last_error}")


def timed_action(steps: list[dict[str, Any]], name: str, fn: Any, *, phase: str | None = None) -> Any:
    started = time.perf_counter()
    try:
        value = fn()
    except Exception as exc:
        steps.append(
            {
                "name": name,
                "kind": "action",
                "ok": False,
                "wall_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": str(exc),
                **({"phase": phase} if phase is not None else {}),
            }
        )
        raise
    steps.append(
        {
            "name": name,
            "kind": "action",
            "ok": True,
            "wall_ms": round((time.perf_counter() - started) * 1000, 3),
            **({"phase": phase} if phase is not None else {}),
        }
    )
    return value


def parse_json_step(result: subprocess.CompletedProcess[str], name: str) -> dict[str, Any]:
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BenchFailure(f"{name} did not return JSON: {exc}") from exc
    if not data.get("ok", False):
        raise BenchFailure(f"{name} returned ok=false: {data.get('diagnostics')}")
    return data


def make_fixture_tree(path: Path, *, file_count: int, file_size: int) -> None:
    src = path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("agent benchmark fixture\n")
    for index in range(file_count):
        header = f"fixture file {index:04d}\n"
        body = (header + ("x" * max(0, file_size - len(header))) + "\n")[:file_size]
        (src / f"file_{index:04d}.txt").write_text(body)


def init_git_repo(steps: list[dict[str, Any]], repo: Path, *, file_count: int, file_size: int) -> None:
    timed_action(steps, "fixture.create", lambda: make_fixture_tree(repo, file_count=file_count, file_size=file_size))
    run_command(steps, "git.init", ["git", "init", "-b", "main"], cwd=repo)
    run_command(steps, "git.config.email", ["git", "config", "user.email", "bench@example.com"], cwd=repo)
    run_command(steps, "git.config.name", ["git", "config", "user.name", "Tri Bench"], cwd=repo)
    run_command(steps, "git.add.initial", ["git", "add", "."], cwd=repo)
    run_command(steps, "git.commit.initial", ["git", "commit", "-m", "initial"], cwd=repo)


def mutation_specs(attempt: int, mutations_per_attempt: int) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for mutation in range(mutations_per_attempt):
        if mutations_per_attempt == 1:
            path = f"src/agent_{attempt}.txt"
            text = f"agent {attempt} attempt\n"
        else:
            path = f"src/agent_{attempt}_{mutation}.txt"
            text = f"agent {attempt} mutation {mutation}\n"
        specs.append({"path": path, "text": text})
    return specs


def check_mutations_command(specs: list[dict[str, str]]) -> list[str]:
    pairs = [(spec["path"], spec["text"]) for spec in specs]
    code = (
        "from pathlib import Path\n"
        f"pairs = {pairs!r}\n"
        "for path, text in pairs:\n"
        "    assert Path(path).read_text().startswith(text), path\n"
    )
    return [sys.executable, "-c", code]


def load_workflow_scenario(name_or_path: str = DEFAULT_SCENARIO) -> dict[str, Any]:
    path = Path(name_or_path)
    if not path.exists():
        candidate = SCENARIO_DIR / f"{name_or_path}.json"
        if candidate.exists():
            path = candidate
    if not path.exists():
        available = ", ".join(sorted(item.stem for item in SCENARIO_DIR.glob("*.json")))
        raise argparse.ArgumentTypeError(f"unknown workflow scenario {name_or_path!r}; available: {available}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid workflow scenario {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise argparse.ArgumentTypeError(f"workflow scenario {path} must be a JSON object")
    data = dict(data)
    data["id"] = path.stem
    data["path"] = str(path)
    return data


def apply_workflow_scenario(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    scenario = load_workflow_scenario(args.scenario)
    args.scenario = scenario
    if args.targets is None:
        raw_targets = scenario.get("targets", parse_targets("tri,git,svn,gitbutler,entire"))
        args.targets = parse_targets(",".join(raw_targets) if isinstance(raw_targets, list) else str(raw_targets))
    if args.repeats is None:
        args.repeats = int(scenario.get("repeats", 1))
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    for field, minimum in (("attempts", 1), ("files", 1), ("file_size", 32), ("mutations_per_attempt", 1)):
        if getattr(args, field) is None:
            setattr(
                args,
                field,
                int(scenario.get(field, {"attempts": 3, "files": 100, "file_size": 1024, "mutations_per_attempt": 1}[field])),
            )
        if getattr(args, field) < minimum:
            option = "--file-size" if field == "file_size" else f"--{field.replace('_', '-')}"
            parser.error(f"{option} must be at least {minimum}")
    if args.verify_tasks is None:
        args.verify_tasks = bool(scenario.get("verify_tasks", False))
    if args.verify_task is None and scenario.get("verify_task_ids"):
        raw_task_ids = scenario["verify_task_ids"]
        args.verify_task = raw_task_ids if isinstance(raw_task_ids, list) else [str(raw_task_ids)]


def infer_phase(step: dict[str, Any]) -> str:
    explicit = step.get("phase")
    if explicit in PHASES:
        return str(explicit)
    name = str(step.get("name", ""))
    if name.startswith(SETUP_STEP_PREFIXES):
        return "setup"
    if name.startswith(EXPORT_STEP_PREFIXES):
        return "export"
    if name.startswith("task-verifier."):
        return "verify"
    return "workflow"


def phase_wall_ms(steps: list[dict[str, Any]]) -> dict[str, float]:
    phases = {phase: 0.0 for phase in PHASES}
    for step in steps:
        phases[infer_phase(step)] += float(step.get("wall_ms", 0))
    return {phase: round(value, 3) for phase, value in phases.items()}


def summarize_steps(steps: list[dict[str, Any]], *, target_wall_ms: float, store_bytes: int = 0) -> dict[str, Any]:
    stdin_bytes = sum(int(step.get("stdin_bytes", 0)) for step in steps)
    stdout_bytes = sum(int(step.get("stdout_bytes", 0)) for step in steps)
    stderr_bytes = sum(int(step.get("stderr_bytes", 0)) for step in steps)
    response_bytes = sum(int(step.get("response_bytes", 0)) for step in steps)
    transport_bytes = stdin_bytes + stdout_bytes + stderr_bytes
    phases = phase_wall_ms(steps)
    return {
        "wall_ms": round(target_wall_ms, 3),
        "step_wall_ms": round(sum(float(step.get("wall_ms", 0)) for step in steps), 3),
        "phase_wall_ms": phases,
        "setup_wall_ms": phases["setup"],
        "workflow_wall_ms": phases["workflow"],
        "export_wall_ms": phases["export"],
        "verify_wall_ms": phases["verify"],
        "commands": sum(1 for step in steps if step.get("kind") == "command"),
        "external_commands": sum(1 for step in steps if step.get("kind") == "command"),
        "requests": sum(1 for step in steps if step.get("kind") == "http"),
        "http_requests": sum(1 for step in steps if step.get("kind") == "http"),
        "embedded_calls": sum(1 for step in steps if step.get("kind") == "embedded"),
        "actions": sum(1 for step in steps if step.get("kind") == "action"),
        "stdin_bytes": stdin_bytes,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "response_bytes": response_bytes,
        "transport_bytes": transport_bytes,
        "agent_io_bytes": transport_bytes + response_bytes,
        "store_bytes": store_bytes,
    }


def failed_result(
    target: str,
    error: str,
    steps: list[dict[str, Any]],
    *,
    target_wall_ms: float,
    store_bytes: int = 0,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "target": target,
        "ok": False,
        "skipped": False,
        "error": error,
        "details": details or {},
        "totals": summarize_steps(steps, target_wall_ms=target_wall_ms, store_bytes=store_bytes),
        "steps": steps,
    }


def write_codex_transcript(path: Path, *, session_id: str, prompt: str) -> None:
    rows = [
        {"type": "session_meta", "id": session_id, "timestamp": utcnow()},
        {"type": "message", "role": "user", "content": prompt},
        {"type": "message", "role": "assistant", "content": "Benchmark attempt completed."},
    ]
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")


def tri_json(steps: list[dict[str, Any]], name: str, args: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    result = run_command(steps, name, [sys.executable, "-m", "tri", *args], cwd=cwd, env=env)
    data = parse_json_step(result, name)
    steps[-1]["schema"] = data.get("schema")
    steps[-1]["tri_cost"] = data.get("cost")
    steps[-1]["tri_events"] = len(data.get("events", []))
    return data


def tri_embedded_batch(steps: list[dict[str, Any]], name: str, payload: dict[str, Any], *, tri_home: Path) -> dict[str, Any]:
    from tri.cli import Store, envelope, response_json, run_batch_payload

    started = time.perf_counter()
    output: dict[str, Any] = {}
    error = ""
    try:
        store = Store(home=tri_home)
        try:
            data, ok = run_batch_payload(store, payload, default_actor="actor_local")
            output = envelope("tri.batch", ["tri", "--response", "summary", "batch", "<embedded>"], ok=ok, data=data)
        finally:
            store.close()
    except Exception as exc:
        error = str(exc)
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    encoded = response_json(output).encode() if output else b""
    ok = bool(output.get("ok", False)) and not error
    steps.append(
        {
            "name": name,
            "kind": "embedded",
            "ok": ok,
            "wall_ms": wall_ms,
            "stdin_bytes": 0,
            "stdout_bytes": 0,
            "stderr_bytes": len(error.encode()),
            "response_bytes": len(encoded),
            "schema": output.get("schema"),
            "tri_events": len(output.get("events", [])),
        }
    )
    if error or not ok:
        diagnostics = output.get("diagnostics") if output else None
        raise BenchFailure(f"{name} failed in embedded mode: error={error} diagnostics={diagnostics}")
    return output


def tri_cli_batch(steps: list[dict[str, Any]], name: str, plan_path: Path, *, cwd: Path, tri_home: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["TRI_HOME"] = str(tri_home)
    result = run_command(steps, name, [sys.executable, "-m", "tri", "--response", "summary", "batch", str(plan_path)], cwd=cwd, env=env)
    data = parse_json_step(result, name)
    steps[-1]["schema"] = data.get("schema")
    steps[-1]["tri_events"] = len(data.get("events", []))
    return data


def tri_http_batch(steps: list[dict[str, Any]], name: str, payload: dict[str, Any], *, cwd: Path, tri_home: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["TRI_HOME"] = str(tri_home)
    port = free_port()
    proc = run_process(
        steps,
        "tri.serve.start",
        [sys.executable, "-m", "tri", "--response", "summary", "serve", "--host", "127.0.0.1", "--port", str(port), "--quiet"],
        cwd=cwd,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_health(steps, "tri.serve.health", f"{base}/v0/health", proc)
        return http_json(steps, name, f"{base}/v0/batch", payload=payload, actor="actor_local")
    finally:
        stop_process(steps, "tri.serve.stop", proc)


def prepare_tri_batch(steps: list[dict[str, Any]], workdir: Path, args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any], int]:
    repo = workdir / "tri-source"
    repo.mkdir()
    timed_action(steps, "fixture.create", lambda: make_fixture_tree(repo, file_count=args.files, file_size=args.file_size))
    tri_home = workdir / "tri-home"
    commands: list[dict[str, Any]] = [
        {"as": "import", "response": "summary", "argv": ["import", str(repo), "--name", "bench"]},
    ]
    for index in range(args.attempts):
        actor = f"agent:{index}"
        specs = mutation_specs(index, args.mutations_per_attempt)
        commands.extend(
            [
                {
                    "as": f"workspace{index}",
                    "actor": actor,
                    "response": "summary",
                    "argv": ["workspace", "create", "bench", "--from", "${import.data.target.name}", "--task", f"attempt {index}"],
                },
                {
                    "as": f"read{index}",
                    "actor": actor,
                    "response": "summary",
                    "argv": ["files", "read", f"${{workspace{index}.data.id}}", "src/file_0000.txt", "--limit", "256"],
                },
                *[
                    {
                        "as": f"write{index}_{mutation}",
                        "actor": actor,
                        "response": "summary",
                        "argv": ["files", "write", f"${{workspace{index}.data.id}}", spec["path"], "--text", spec["text"]],
                    }
                    for mutation, spec in enumerate(specs)
                ],
                {
                    "as": f"check{index}",
                    "actor": actor,
                    "response": "summary",
                    "argv": ["run", f"${{workspace{index}.data.id}}", "--no-import-changes", "--", *check_mutations_command(specs)],
                },
                {
                    "as": f"changeset{index}",
                    "actor": actor,
                    "response": "summary",
                    "argv": ["changeset", "create", f"${{workspace{index}.data.id}}", "--title", f"Agent {index}"],
                },
            ]
        )
    if args.attempts > 1:
        commands.append(
            {
                "as": "compare",
                "response": "summary",
                "argv": ["changeset", "compare", *[f"${{changeset{index}.data.id}}" for index in range(args.attempts)]],
            }
        )
    commands.extend(
        [
            {"as": "review", "response": "summary", "argv": ["review", "create", "${changeset0.data.id}"]},
            {"as": "approve", "response": "summary", "argv": ["review", "approve", "${review.data.id}"]},
            {
                "as": "export",
                "response": "summary",
                "argv": ["export", "git-patch", "${changeset0.data.id}", "--output", str(workdir / "tri-export.patch"), "--summary"],
            },
            {"as": "doctor", "response": "summary", "argv": ["doctor"]},
        ]
    )
    plan_path = workdir / "tri-batch.json"
    returns = {
        "changesets": [f"${{changeset{index}.data.id}}" for index in range(args.attempts)],
        "doctor_counts": "${doctor.data.counts}",
    }
    payload = {"commands": commands, "return": returns, "report": "returns"}
    timed_action(steps, "tri.batch.plan", lambda: plan_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":"))))
    return tri_home, plan_path, payload, len(commands)


def tri_benchmark_result(
    *,
    target: str,
    steps: list[dict[str, Any]],
    started: float,
    tri_home: Path,
    batch: dict[str, Any],
    transport: str,
    batch_commands: int,
) -> dict[str, Any]:
    returned = batch["data"]["returns"]
    changesets = returned["changesets"]
    wall_ms = (time.perf_counter() - started) * 1000
    return {
        "target": target,
        "ok": True,
        "skipped": False,
        "details": {
            "transport": transport,
            "batch_commands": batch_commands,
            "changesets": changesets,
            "doctor_counts": returned["doctor_counts"],
        },
        "totals": summarize_steps(steps, target_wall_ms=wall_ms, store_bytes=directory_size(tri_home)),
        "steps": steps,
    }


def bench_tri(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()

    tri_home, _plan_path, payload, batch_commands = prepare_tri_batch(steps, workdir, args)
    batch = tri_embedded_batch(steps, "tri.batch", payload, tri_home=tri_home)
    return tri_benchmark_result(target="tri", steps=steps, started=started, tri_home=tri_home, batch=batch, transport="embedded_batch", batch_commands=batch_commands)


def bench_tri_cli(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()

    tri_home, plan_path, _payload, batch_commands = prepare_tri_batch(steps, workdir, args)
    batch = tri_cli_batch(steps, "tri.batch.cli", plan_path, cwd=workdir, tri_home=tri_home)
    return tri_benchmark_result(target="tri-cli", steps=steps, started=started, tri_home=tri_home, batch=batch, transport="cli_batch", batch_commands=batch_commands)


def bench_tri_http(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()

    tri_home, _plan_path, payload, batch_commands = prepare_tri_batch(steps, workdir, args)
    batch = tri_http_batch(steps, "tri.batch.http", payload, cwd=workdir, tri_home=tri_home)
    return tri_benchmark_result(target="tri-http", steps=steps, started=started, tri_home=tri_home, batch=batch, transport="http_batch", batch_commands=batch_commands)


def bench_git(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    if not command_exists("git"):
        return skipped_result("git", "git command not found")

    repo = workdir / "git-source"
    repo.mkdir()
    init_git_repo(steps, repo, file_count=args.files, file_size=args.file_size)
    branches: list[str] = []
    worktrees: list[Path] = []

    for index in range(args.attempts):
        branch = f"agent-{index}"
        branches.append(branch)
        worktree = workdir / f"git-wt-{index}"
        worktrees.append(worktree)
        specs = mutation_specs(index, args.mutations_per_attempt)
        paths = [spec["path"] for spec in specs]
        run_command(steps, f"git.worktree.add.{index}", ["git", "worktree", "add", "-b", branch, str(worktree), "main"], cwd=repo)
        for mutation, spec in enumerate(specs):
            path = worktree / spec["path"]
            timed_action(steps, f"fs.write.{index}.{mutation}", lambda p=path, t=spec["text"]: p.write_text(t))
        timed_action(steps, f"fs.read.{index}", lambda p=worktree / "src" / "file_0000.txt": p.read_text())
        run_command(steps, f"git.diff.{index}", ["git", "diff", "--", *paths], cwd=worktree)
        run_command(steps, f"check.{index}", check_mutations_command(specs), cwd=worktree)
        run_command(steps, f"git.add.{index}", ["git", "add", *paths], cwd=worktree)
        run_command(steps, f"git.commit.{index}", ["git", "commit", "-m", f"agent {index}"], cwd=worktree)

    if len(branches) > 1:
        run_command(steps, "git.compare.branches", ["git", "diff", "--name-only", f"{branches[0]}...{branches[1]}"], cwd=repo)
    patch = run_command(steps, "git.format_patch", ["git", "format-patch", "-1", "--stdout", branches[0]], cwd=repo)
    timed_action(steps, "git.patch.write", lambda: (workdir / "git-export.patch").write_text(patch.stdout))
    wall_ms = (time.perf_counter() - started) * 1000
    return {
        "target": "git",
        "ok": True,
        "skipped": False,
        "details": {"branches": branches},
        "totals": summarize_steps(steps, target_wall_ms=wall_ms, store_bytes=directories_size([repo, *worktrees])),
        "steps": steps,
    }


def git_batch_runner_source(args: argparse.Namespace) -> str:
    config = {
        "attempts": args.attempts,
        "files": args.files,
        "file_size": args.file_size,
        "mutations_per_attempt": args.mutations_per_attempt,
    }
    return f"""
import json
import subprocess
import sys
import time
from pathlib import Path

CONFIG = {config!r}
ROOT = Path(__file__).resolve().parent
PHASES = {{"setup": 0.0, "workflow": 0.0, "export": 0.0}}
INTERNAL_COMMANDS = 0


def timed(phase, fn):
    started = time.perf_counter()
    result = fn()
    PHASES[phase] += (time.perf_counter() - started) * 1000
    return result


def run(argv, cwd):
    global INTERNAL_COMMANDS
    INTERNAL_COMMANDS += 1
    result = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(argv) + "\\n" + result.stderr[-2000:])
    return result


def make_fixture_tree(path, file_count, file_size):
    src = path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("agent benchmark fixture\\n")
    for index in range(file_count):
        header = f"fixture file {{index:04d}}\\n"
        body = (header + ("x" * max(0, file_size - len(header))) + "\\n")[:file_size]
        (src / f"file_{{index:04d}}.txt").write_text(body)


def mutation_specs(attempt, mutations_per_attempt):
    specs = []
    for mutation in range(mutations_per_attempt):
        if mutations_per_attempt == 1:
            path = f"src/agent_{{attempt}}.txt"
            text = f"agent {{attempt}} attempt\\n"
        else:
            path = f"src/agent_{{attempt}}_{{mutation}}.txt"
            text = f"agent {{attempt}} mutation {{mutation}}\\n"
        specs.append({{"path": path, "text": text}})
    return specs


repo = ROOT / "git-batch-source"
repo.mkdir()
timed("setup", lambda: make_fixture_tree(repo, CONFIG["files"], CONFIG["file_size"]))
timed("setup", lambda: run(["git", "init", "-b", "main"], repo))
timed("setup", lambda: run(["git", "config", "user.email", "bench@example.com"], repo))
timed("setup", lambda: run(["git", "config", "user.name", "Tri Bench"], repo))
timed("setup", lambda: run(["git", "add", "."], repo))
timed("setup", lambda: run(["git", "commit", "-m", "initial"], repo))

branches = []
worktrees = []
for index in range(CONFIG["attempts"]):
    branch = f"agent-{{index}}"
    branches.append(branch)
    worktree = ROOT / f"git-batch-wt-{{index}}"
    worktrees.append(str(worktree))
    specs = mutation_specs(index, CONFIG["mutations_per_attempt"])
    paths = [spec["path"] for spec in specs]
    timed("workflow", lambda branch=branch, worktree=worktree: run(["git", "worktree", "add", "-b", branch, str(worktree), "main"], repo))
    for spec in specs:
        timed("workflow", lambda spec=spec, worktree=worktree: (worktree / spec["path"]).write_text(spec["text"]))
    timed("workflow", lambda worktree=worktree: (worktree / "src" / "file_0000.txt").read_text())
    timed("workflow", lambda paths=paths, worktree=worktree: run(["git", "diff", "--", *paths], worktree))
    def check(specs=specs, worktree=worktree):
        for spec in specs:
            assert (worktree / spec["path"]).read_text().startswith(spec["text"]), spec["path"]
    timed("workflow", check)
    timed("workflow", lambda paths=paths, worktree=worktree: run(["git", "add", *paths], worktree))
    timed("workflow", lambda index=index, worktree=worktree: run(["git", "commit", "-m", f"agent {{index}}"], worktree))

if len(branches) > 1:
    timed("export", lambda: run(["git", "diff", "--name-only", f"{{branches[0]}}...{{branches[1]}}"], repo))
patch = timed("export", lambda: run(["git", "format-patch", "-1", "--stdout", branches[0]], repo))
timed("export", lambda: (ROOT / "git-batch-export.patch").write_text(patch.stdout))

print(json.dumps({{
    "ok": True,
    "branches": branches,
    "worktrees": worktrees,
    "internal_commands": INTERNAL_COMMANDS,
    "phase_wall_ms": {{key: round(value, 3) for key, value in PHASES.items()}},
}}, sort_keys=True, separators=(",", ":")))
"""


def apply_phase_overrides(totals: dict[str, Any], phase_overrides: dict[str, Any]) -> dict[str, Any]:
    phase_values = {phase: round(float(phase_overrides.get(phase, 0)), 3) for phase in PHASES}
    totals = dict(totals)
    totals["phase_wall_ms"] = phase_values
    for phase, value in phase_values.items():
        totals[f"{phase}_wall_ms"] = value
    return totals


def bench_git_batch(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    if not command_exists("git"):
        return skipped_result("git-batch", "git command not found")

    script = workdir / "git-batch-runner.py"
    timed_action(steps, "git.batch.plan", lambda: script.write_text(git_batch_runner_source(args)))
    result = run_command(steps, "git.batch.run", [sys.executable, str(script)], cwd=workdir, phase="workflow")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BenchFailure(f"git-batch did not return JSON: {exc}") from exc
    if not data.get("ok"):
        raise BenchFailure(f"git-batch returned ok=false: {data}")

    repo = workdir / "git-batch-source"
    worktrees = [Path(path) for path in data.get("worktrees", [])]
    wall_ms = (time.perf_counter() - started) * 1000
    totals = summarize_steps(steps, target_wall_ms=wall_ms, store_bytes=directories_size([repo, *worktrees]))
    totals = apply_phase_overrides(totals, data.get("phase_wall_ms", {}))
    return {
        "target": "git-batch",
        "ok": True,
        "skipped": False,
        "details": {
            "branches": data.get("branches", []),
            "internal_commands": data.get("internal_commands", 0),
            "note": "Batched Git worktree workflow in one Python subprocess; internal Git commands are hidden from agent control-plane metrics.",
        },
        "totals": totals,
        "steps": steps,
    }


def bench_svn(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    if not command_exists("svn") or not command_exists("svnadmin"):
        return skipped_result("svn", "svn and svnadmin are required")

    fixture = workdir / "svn-fixture"
    fixture.mkdir()
    timed_action(steps, "fixture.create", lambda: make_fixture_tree(fixture, file_count=args.files, file_size=args.file_size))
    repo = workdir / "svn-repo"
    run_command(steps, "svnadmin.create", ["svnadmin", "create", str(repo)], cwd=workdir)
    repo_uri = repo.resolve().as_uri()
    run_command(steps, "svn.import.trunk", ["svn", "import", str(fixture), f"{repo_uri}/trunk", "-m", "initial"], cwd=workdir)
    run_command(steps, "svn.mkdir.branches", ["svn", "mkdir", f"{repo_uri}/branches", "-m", "branches"], cwd=workdir)

    branches: list[str] = []
    workcopies: list[Path] = []
    for index in range(args.attempts):
        branch_uri = f"{repo_uri}/branches/agent-{index}"
        branches.append(branch_uri)
        workcopy = workdir / f"svn-wc-{index}"
        workcopies.append(workcopy)
        specs = mutation_specs(index, args.mutations_per_attempt)
        paths = [spec["path"] for spec in specs]
        run_command(steps, f"svn.copy.branch.{index}", ["svn", "copy", f"{repo_uri}/trunk", branch_uri, "-m", f"agent {index} branch"], cwd=workdir)
        run_command(steps, f"svn.checkout.{index}", ["svn", "checkout", branch_uri, str(workcopy)], cwd=workdir)
        for mutation, spec in enumerate(specs):
            path = workcopy / spec["path"]
            timed_action(steps, f"fs.write.{index}.{mutation}", lambda p=path, t=spec["text"]: p.write_text(t))
        timed_action(steps, f"fs.read.{index}", lambda p=workcopy / "src" / "file_0000.txt": p.read_text())
        run_command(steps, f"check.{index}", check_mutations_command(specs), cwd=workcopy)
        run_command(steps, f"svn.add.{index}", ["svn", "add", *paths], cwd=workcopy)
        run_command(steps, f"svn.commit.{index}", ["svn", "commit", "-m", f"agent {index}"], cwd=workcopy)

    if len(branches) > 1:
        run_command(steps, "svn.compare.branches", ["svn", "diff", "--summarize", branches[0], branches[1]], cwd=workdir)
    patch = run_command(steps, "svn.diff.export", ["svn", "diff", f"{repo_uri}/trunk", branches[0]], cwd=workdir)
    timed_action(steps, "svn.patch.write", lambda: (workdir / "svn-export.patch").write_text(patch.stdout))
    wall_ms = (time.perf_counter() - started) * 1000
    return {
        "target": "svn",
        "ok": True,
        "skipped": False,
        "details": {"branches": branches},
        "totals": summarize_steps(steps, target_wall_ms=wall_ms, store_bytes=directories_size([repo, *workcopies])),
        "steps": steps,
    }


def bench_gitbutler(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    exe = shutil.which("but") or shutil.which("gitbutler")
    if exe is None:
        return skipped_result("gitbutler", "GitButler CLI not found (`but` or `gitbutler`)")
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    repo = workdir / "gitbutler-source"
    try:
        repo.mkdir()
        init_git_repo(steps, repo, file_count=args.files, file_size=args.file_size)
        run_command(steps, "gitbutler.version", [exe, "--version"], cwd=repo)
        run_command(steps, "gitbutler.setup", [exe, "setup", "--json"], cwd=repo)
        run_command(steps, "gitbutler.status.initial", [exe, "status", "--json"], cwd=repo)

        branches: list[str] = []
        commits: list[str] = []
        for index in range(args.attempts):
            branch = f"agent-{index}"
            branches.append(branch)
            specs = mutation_specs(index, args.mutations_per_attempt)
            run_command(steps, f"gitbutler.branch.new.{index}", [exe, "branch", "new", branch, "--json", "--status-after"], cwd=repo)
            for mutation, spec in enumerate(specs):
                path = repo / spec["path"]
                timed_action(steps, f"fs.write.{index}.{mutation}", lambda p=path, t=spec["text"]: p.write_text(t))
            timed_action(steps, f"fs.read.{index}", lambda p=repo / "src" / "file_0000.txt": p.read_text())
            run_command(steps, f"gitbutler.diff.{index}", [exe, "diff", "--json"], cwd=repo)
            run_command(steps, f"check.{index}", check_mutations_command(specs), cwd=repo)
            commit = run_command(
                steps,
                f"gitbutler.commit.{index}",
                [exe, "commit", branch, "-m", f"agent {index}", "--json", "--status-after"],
                cwd=repo,
            )
            try:
                commit_data = json.loads(commit.stdout)
                commits.append(commit_data.get("result", {}).get("commit_id", ""))
            except json.JSONDecodeError:
                commits.append("")

        if len(branches) > 1:
            run_command(steps, "gitbutler.compare.branches", ["git", "diff", "--name-only", f"{branches[0]}...{branches[1]}"], cwd=repo)
        run_command(steps, "gitbutler.branch.list", [exe, "branch", "list", "--json", "--all", "--no-check", "--no-ahead"], cwd=repo)
        run_command(steps, "gitbutler.show.first", [exe, "show", branches[0], "--json"], cwd=repo)
        run_command(steps, "gitbutler.oplog.snapshot", [exe, "oplog", "snapshot", "-m", "agentic benchmark", "--json"], cwd=repo)
        run_command(steps, "gitbutler.oplog.list", [exe, "oplog", "list", "--json"], cwd=repo)
        patch = run_command(steps, "gitbutler.format_patch", ["git", "format-patch", "-1", "--stdout", branches[0]], cwd=repo)
        timed_action(steps, "gitbutler.patch.write", lambda: (workdir / "gitbutler-export.patch").write_text(patch.stdout))
        wall_ms = (time.perf_counter() - started) * 1000
        return {
            "target": "gitbutler",
            "ok": True,
            "skipped": False,
            "details": {
                "branches": branches,
                "commits": [commit for commit in commits if commit],
                "note": "Uses GitButler CLI branch stacks and operation log; patch export uses Git compatibility.",
            },
            "totals": summarize_steps(steps, target_wall_ms=wall_ms, store_bytes=directory_size(repo)),
            "steps": steps,
        }
    except Exception as exc:
        wall_ms = (time.perf_counter() - started) * 1000
        return failed_result(
            "gitbutler",
            str(exc),
            steps,
            target_wall_ms=wall_ms,
            store_bytes=directory_size(repo),
        )


def bench_entire(workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    exe = shutil.which("entire")
    if exe is None:
        return skipped_result("entire", "Entire CLI not found (`entire`)")
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    repo = workdir / "entire-source"
    env = os.environ.copy()
    env["ACCESSIBLE"] = "1"
    env["NO_COLOR"] = "1"
    env["PAGER"] = "cat"
    transcripts = workdir / "entire-transcripts"
    try:
        repo.mkdir()
        transcripts.mkdir()
        init_git_repo(steps, repo, file_count=args.files, file_size=args.file_size)
        run_command(steps, "entire.version", [exe, "--version"], cwd=repo, env=env)
        run_command(
            steps,
            "entire.enable.codex",
            [exe, "enable", "--agent", "codex", "--yes", "--skip-push-sessions", "--telemetry=false", "--local"],
            cwd=repo,
            env=env,
        )
        run_command(steps, "entire.status.initial", [exe, "status", "--json"], cwd=repo, env=env)
        run_command(steps, "git.add.entire.config", ["git", "add", ".codex", ".entire"], cwd=repo)
        run_command(steps, "git.commit.entire.config", ["git", "commit", "-m", "entire setup"], cwd=repo)

        branches: list[str] = []
        session_ids: list[str] = []
        checkpoint_counts: dict[str, int] = {}
        for index in range(args.attempts):
            branch = f"agent-{index}"
            branches.append(branch)
            session_id = f"bench-session-{index}"
            session_ids.append(session_id)
            transcript = transcripts / f"{session_id}.jsonl"
            prompt = f"Implement attempt {index} for the benchmark."
            timed_action(steps, f"entire.transcript.write.{index}", lambda p=transcript, sid=session_id, pr=prompt: write_codex_transcript(p, session_id=sid, prompt=pr))
            hook_base = {
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": str(repo),
                "model": "gpt-5-codex",
            }
            run_command(
                steps,
                f"entire.hook.session_start.{index}",
                [exe, "hooks", "codex", "session-start"],
                cwd=repo,
                env=env,
                stdin=json.dumps(hook_base),
            )
            prompt_payload = {**hook_base, "prompt": prompt}
            run_command(
                steps,
                f"entire.hook.user_prompt.{index}",
                [exe, "hooks", "codex", "user-prompt-submit"],
                cwd=repo,
                env=env,
                stdin=json.dumps(prompt_payload),
            )
            run_command(steps, f"git.checkout.branch.{index}", ["git", "checkout", "-b", branch, "main"], cwd=repo)
            specs = mutation_specs(index, args.mutations_per_attempt)
            paths = [spec["path"] for spec in specs]
            for mutation, spec in enumerate(specs):
                path = repo / spec["path"]
                timed_action(steps, f"fs.write.{index}.{mutation}", lambda p=path, t=spec["text"]: p.write_text(t))
            timed_action(steps, f"fs.read.{index}", lambda p=repo / "src" / "file_0000.txt": p.read_text())
            run_command(steps, f"git.diff.{index}", ["git", "diff", "--", *paths], cwd=repo)
            run_command(steps, f"check.{index}", check_mutations_command(specs), cwd=repo)
            stop_payload = {**hook_base, "stop_hook_active": False}
            run_command(
                steps,
                f"entire.hook.stop.{index}",
                [exe, "hooks", "codex", "stop"],
                cwd=repo,
                env=env,
                stdin=json.dumps(stop_payload),
            )
            run_command(steps, f"git.add.{index}", ["git", "add", *paths], cwd=repo)
            run_command(steps, f"git.commit.{index}", ["git", "commit", "-m", f"agent {index}"], cwd=repo)
            checkpoints = run_command(
                steps,
                f"entire.checkpoint.explain.{index}",
                [exe, "checkpoint", "explain", "--json", "--limit", str(max(10, args.attempts))],
                cwd=repo,
                env=env,
            )
            try:
                checkpoint_counts[branch] = len(json.loads(checkpoints.stdout))
            except json.JSONDecodeError:
                checkpoint_counts[branch] = -1
            run_command(steps, f"git.checkout.main.{index}", ["git", "checkout", "main"], cwd=repo)

        if len(branches) > 1:
            run_command(steps, "entire.compare.branches", ["git", "diff", "--name-only", f"{branches[0]}...{branches[1]}"], cwd=repo)
        run_command(steps, "entire.session.list", [exe, "session", "list", "--json"], cwd=repo, env=env)
        run_command(steps, "entire.status.final", [exe, "status", "--json"], cwd=repo, env=env)
        run_command(steps, "entire.doctor", [exe, "doctor", "--force"], cwd=repo, env=env)
        patch = run_command(steps, "entire.format_patch", ["git", "format-patch", "-1", "--stdout", branches[0]], cwd=repo)
        timed_action(steps, "entire.patch.write", lambda: (workdir / "entire-export.patch").write_text(patch.stdout))
        wall_ms = (time.perf_counter() - started) * 1000
        return {
            "target": "entire",
            "ok": True,
            "skipped": False,
            "details": {
                "branches": branches,
                "sessions": session_ids,
                "checkpoint_counts_by_branch": checkpoint_counts,
                "note": "Uses Entire's Codex hook handlers to create real local checkpoints, then commits the same file changes with Git.",
            },
            "totals": summarize_steps(steps, target_wall_ms=wall_ms, store_bytes=directories_size([repo, transcripts])),
            "steps": steps,
        }
    except Exception as exc:
        wall_ms = (time.perf_counter() - started) * 1000
        return failed_result(
            "entire",
            str(exc),
            steps,
            target_wall_ms=wall_ms,
            store_bytes=directories_size([repo, transcripts]),
        )


def skipped_result(target: str, reason: str) -> dict[str, Any]:
    return {
        "target": target,
        "ok": False,
        "skipped": True,
        "skip_reason": reason,
        "details": {},
        "totals": summarize_steps([], target_wall_ms=0),
        "steps": [],
    }


TARGETS = {
    "tri": bench_tri,
    "tri-cli": bench_tri_cli,
    "tri-http": bench_tri_http,
    "git": bench_git,
    "git-batch": bench_git_batch,
    "svn": bench_svn,
    "gitbutler": bench_gitbutler,
    "entire": bench_entire,
}


def target_result(target: str, fn: Any, workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    try:
        return fn(workdir, args)
    except Exception as exc:
        return {
            "target": target,
            "ok": False,
            "skipped": False,
            "error": str(exc),
            "details": {},
            "totals": summarize_steps([], target_wall_ms=0),
            "steps": [],
        }


def median_number(values: list[float]) -> float:
    return round(float(statistics.median(values)), 3)


def mean_number(values: list[float]) -> float:
    return round(float(statistics.fmean(values)), 3)


def aggregate_target_results(target: str, repeated: list[dict[str, Any]]) -> dict[str, Any]:
    skipped = [item for item in repeated if item.get("skipped")]
    failed = [item for item in repeated if not item.get("ok") and not item.get("skipped")]
    ok = [item for item in repeated if item.get("ok")]
    baseline = ok[0] if ok else repeated[0]

    if len(repeated) == 1:
        single = dict(baseline)
        single["details"] = dict(single.get("details", {}))
        single["details"]["repeats"] = 1
        single["details"]["ok_repeats"] = len(ok)
        single["details"]["failed_repeats"] = len(failed)
        single["details"]["skipped_repeats"] = len(skipped)
        return single

    totals: dict[str, Any] = {}
    if ok:
        metric_names = sorted({key for item in ok for key in item.get("totals", {})})
        for name in metric_names:
            values = [float(item["totals"][name]) for item in ok if isinstance(item.get("totals", {}).get(name), (int, float))]
            if not values:
                continue
            median = median_number(values)
            mean = mean_number(values)
            min_value = round(min(values), 3)
            max_value = round(max(values), 3)
            if name in COUNT_METRICS:
                totals[name] = int(median)
                totals[f"{name}_min"] = int(min_value)
                totals[f"{name}_max"] = int(max_value)
                totals[f"{name}_mean"] = mean if not mean.is_integer() else int(mean)
            else:
                totals[name] = median
                totals[f"{name}_mean"] = mean
                totals[f"{name}_min"] = min_value
                totals[f"{name}_max"] = max_value
    else:
        totals = summarize_steps([], target_wall_ms=0)

    details = dict(baseline.get("details", {}))
    wall_values = [float(item.get("totals", {}).get("wall_ms", 0)) for item in ok]
    if wall_values:
        details["wall_ms"] = {
            "median": median_number(wall_values),
            "mean": mean_number(wall_values),
            "min": round(min(wall_values), 3),
            "max": round(max(wall_values), 3),
        }
    details.update(
        {
            "repeats": len(repeated),
            "ok_repeats": len(ok),
            "failed_repeats": len(failed),
            "skipped_repeats": len(skipped),
            "statuses": [
                "ok" if item.get("ok") else ("skipped" if item.get("skipped") else "failed")
                for item in repeated
            ],
        }
    )

    result = {
        "target": target,
        "ok": len(ok) == len(repeated),
        "skipped": len(skipped) == len(repeated),
        "details": details,
        "totals": totals,
        "steps": [],
    }
    if failed:
        result["error"] = "; ".join(str(item.get("error") or item.get("skip_reason") or "failed") for item in failed[:3])
    if len(skipped) == len(repeated):
        result["skip_reason"] = skipped[0].get("skip_reason", "skipped")
    return result


def aggregate_runs(runs: list[dict[str, Any]], targets: list[str]) -> list[dict[str, Any]]:
    aggregated: list[dict[str, Any]] = []
    for target in targets:
        repeated = []
        for run in runs:
            for item in run["targets"]:
                if item["target"] == target:
                    repeated.append(item)
                    break
        if repeated:
            aggregated.append(aggregate_target_results(target, repeated))
    return aggregated


def run_task_verifiers(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    from benchmarks import task_runner

    tasks_root = args.tasks_root.resolve()
    task_dirs = task_runner.discover_tasks(tasks_root)
    selected = task_runner.select_tasks(task_dirs, args.verify_task)
    for task_dir in selected:
        task_runner.validate_task_dir(task_dir)
    run_root = workdir / "task-verifiers"
    run_root.mkdir(parents=True, exist_ok=True)
    tasks = [
        task_runner.run_task(task_dir, run_root=run_root, keep_workdir=args.keep_workdir)
        for task_dir in selected
    ]
    return {
        "schema": task_runner.SCHEMA,
        "created_at": utcnow(),
        "config": {
            "tasks_root": str(tasks_root),
            "task_ids": [task_runner.task_id(path) for path in selected],
            "keep_workdir": args.keep_workdir,
            "run_root": str(run_root) if args.keep_workdir else None,
        },
        "tasks": tasks,
    }


def verification_summary_lines(verification: dict[str, Any]) -> list[str]:
    tasks = verification.get("tasks", [])
    passed = sum(1 for item in tasks if item.get("ok"))
    reward = sum(float(item.get("reward", 0)) for item in tasks)
    lines = [
        "",
        "## Tri Task Verifiers",
        "",
        f"Passed `{passed}` of `{len(tasks)}` task verifiers. Total reward: `{reward:.3f}`.",
        "",
        "| Task | Status | Reward | Wall ms | Category |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in tasks:
        status = "ok" if item.get("ok") else ("timeout" if item.get("timed_out") else "failed")
        category = item.get("metadata", {}).get("category", "")
        lines.append(f"| {item['task_id']} | {status} | {float(item.get('reward', 0)):.3f} | {float(item.get('wall_ms', 0)):.3f} | {category} |")
    return lines


def markdown_summary(results: dict[str, Any]) -> str:
    lines = [
        "# Agentic Source-Control Benchmark",
        "",
        f"- Created: `{results['created_at']}`",
        f"- Workdir: `{results['workdir']}`",
    ]
    if results["config"].get("scenario"):
        lines.append(f"- Scenario: `{results['config']['scenario']}`")
    lines.extend(
        [
            f"- Repeats: `{results['config'].get('repeats', 1)}`",
            f"- Attempts: `{results['config']['attempts']}`",
            f"- Mutations per attempt: `{results['config'].get('mutations_per_attempt', 1)}`",
            f"- Fixture files: `{results['config']['files']}`",
            f"- File size: `{results['config']['file_size']}` bytes",
            "",
            "| Target | Status | Wall ms | Setup ms | Workflow ms | Export ms | External cmds | HTTP reqs | Embedded calls | Agent I/O bytes | Store bytes | Notes |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in results["targets"]:
        totals = item["totals"]
        status = "ok" if item["ok"] else ("skipped" if item["skipped"] else "failed")
        notes = item.get("skip_reason") or item.get("error") or item.get("details", {}).get("note") or item.get("details", {}).get("transport") or ""
        lines.append(
            f"| {item['target']} | {status} | {totals.get('wall_ms', 0):.3f} | {totals.get('setup_wall_ms', 0):.3f} | {totals.get('workflow_wall_ms', 0):.3f} | {totals.get('export_wall_ms', 0):.3f} | {totals.get('external_commands', totals.get('commands', 0))} | {totals.get('http_requests', totals.get('requests', 0))} | {totals.get('embedded_calls', 0)} | {totals.get('agent_io_bytes', 0)} | {totals.get('store_bytes', 0)} | {notes} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This benchmark is a workflow benchmark, not a pure microbenchmark. It measures an agent-style loop: create isolated attempts, read/write files, run a check, produce reviewable work, compare attempts, and export a patch-like artifact.",
            "",
            "When `Repeats` is greater than 1, table metrics are medians across successful repeats; per-repeat raw results remain in `results.json`.",
            "",
            "`Agent I/O bytes` includes subprocess stdin/stdout/stderr, HTTP request/response/error bodies, and embedded in-process response payloads. `External cmds`, `HTTP reqs`, and `Embedded calls` split the control-plane round trips by transport so embedded Tri work no longer appears as zero interaction.",
            "",
            "`Setup`, `Workflow`, and `Export` phase timings are harness-level timings. Batched transports may report one workflow step unless the target exposes internal phase timing, as `git-batch` does.",
            "",
            "Tri should be judged on structured source-control affordances as well as latency: command count, parseable output, object refs, policy/evidence capture, and whether multiple attempts can proceed without branch/worktree coordination.",
        ]
    )
    if results.get("verification"):
        lines.extend(verification_summary_lines(results["verification"]))
    return "\n".join(lines) + "\n"


def write_results(out_dir: Path, results: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    (out_dir / "summary.md").write_text(markdown_summary(results))


def parse_targets(value: str) -> list[str]:
    targets = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = sorted(set(targets) - set(TARGETS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown benchmark target(s): {', '.join(unknown)}")
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run agentic source-control workflow benchmarks.")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, help="Workflow scenario name or JSON path. Built-ins: default, full, small, stress.")
    parser.add_argument("--targets", type=parse_targets, default=None, help="Comma-separated targets to run. Tri variants: tri, tri-cli, tri-http.")
    parser.add_argument("--repeats", type=int, default=None, help="Number of independent repeats to run and aggregate.")
    parser.add_argument("--attempts", type=int, default=None, help="Number of parallel agent attempts.")
    parser.add_argument("--mutations-per-attempt", type=int, default=None, help="Number of file mutation operations each attempt performs.")
    parser.add_argument("--files", type=int, default=None, help="Number of source fixture files.")
    parser.add_argument("--file-size", type=int, default=None, help="Approximate bytes per fixture file.")
    parser.add_argument("--verify-tasks", action=argparse.BooleanOptionalAction, default=None, help="Run Tri task verifiers and include correctness/security outcomes in the workflow benchmark artifact.")
    parser.add_argument("--verify-task", action="append", help="Task verifier id to run when --verify-tasks is enabled. May be repeated or comma-separated.")
    parser.add_argument("--tasks-root", type=Path, default=ROOT / "benchmarks" / "tasks", help="Directory containing task verifier packages.")
    parser.add_argument("--out", type=Path, default=None, help="Output directory for results.json and summary.md.")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep generated benchmark repositories for inspection.")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    apply_workflow_scenario(args, parser)

    out_dir = args.out or Path("benchmark-results") / time.strftime("%Y%m%d-%H%M%S")
    if args.keep_workdir:
        workdir = Path(tempfile.mkdtemp(prefix="tri-agentic-bench-"))
        cleanup = None
    else:
        temp = tempfile.TemporaryDirectory(prefix="tri-agentic-bench-")
        workdir = Path(temp.name)
        cleanup = temp

    try:
        runs: list[dict[str, Any]] = []
        for repeat in range(args.repeats):
            run_targets = []
            repeat_workdir = workdir / f"repeat-{repeat + 1}"
            repeat_workdir.mkdir()
            for target in args.targets:
                target_workdir = repeat_workdir / target
                target_workdir.mkdir()
                run_targets.append(target_result(target, TARGETS[target], target_workdir, args))
            runs.append({"repeat": repeat + 1, "workdir": str(repeat_workdir), "targets": run_targets})

        verification = run_task_verifiers(args, workdir) if args.verify_tasks else None
        results = {
            "schema": SCHEMA,
            "created_at": utcnow(),
            "workdir": str(workdir),
            "config": {
                "scenario": args.scenario["id"],
                "scenario_path": args.scenario["path"],
                "targets": args.targets,
                "repeats": args.repeats,
                "attempts": args.attempts,
                "mutations_per_attempt": args.mutations_per_attempt,
                "files": args.files,
                "file_size": args.file_size,
                "verify_tasks": args.verify_tasks,
                "verify_task_ids": args.verify_task or [],
                "tasks_root": str(args.tasks_root.resolve()),
                "keep_workdir": args.keep_workdir,
            },
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "tri": shutil.which("tri"),
                "git": shutil.which("git"),
                "svn": shutil.which("svn"),
                "svnadmin": shutil.which("svnadmin"),
                "gitbutler": shutil.which("gitbutler") or shutil.which("but"),
                "entire": shutil.which("entire"),
            },
            "runs": runs,
            "targets": aggregate_runs(runs, args.targets),
        }
        if verification is not None:
            results["verification"] = verification
        write_results(out_dir, results)
        print(markdown_summary(results), end="")
        print(f"Results written to {out_dir}")
        if verification is not None and any(not item.get("ok") for item in verification.get("tasks", [])):
            raise SystemExit(1)
    finally:
        if cleanup is not None:
            cleanup.cleanup()


if __name__ == "__main__":
    main()
