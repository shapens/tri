from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any


SCHEMA = "tri.benchmark.tasks.v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS_ROOT = ROOT / "benchmarks" / "tasks"
REQUIRED_TASK_FILES = [
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "tests/test.sh",
    "solution/solve.sh",
]


class TaskDefinitionError(Exception):
    pass


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_task_toml(task_dir: Path) -> dict[str, Any]:
    try:
        return tomllib.loads((task_dir / "task.toml").read_text())
    except FileNotFoundError as exc:
        raise TaskDefinitionError(f"{task_dir} is missing task.toml") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TaskDefinitionError(f"{task_dir}/task.toml is invalid TOML: {exc}") from exc


def task_id(task_dir: Path, data: dict[str, Any] | None = None) -> str:
    data = data or load_task_toml(task_dir)
    metadata = data.get("metadata", {})
    return str(metadata.get("task_id") or task_dir.name)


def validate_task_dir(task_dir: Path) -> dict[str, Any]:
    missing = [name for name in REQUIRED_TASK_FILES if not (task_dir / name).exists()]
    data = load_task_toml(task_dir) if "task.toml" not in missing else {}
    metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
    required_metadata = ["task_id", "display_title", "category"]
    missing_metadata = [key for key in required_metadata if not metadata.get(key)]
    if missing or missing_metadata:
        raise TaskDefinitionError(
            f"{task_dir} is not a valid benchmark task; "
            f"missing files={missing or []}, missing metadata={missing_metadata or []}"
        )
    return data


def discover_tasks(tasks_root: Path = DEFAULT_TASKS_ROOT) -> list[Path]:
    if not tasks_root.exists():
        return []
    return sorted(path for path in tasks_root.iterdir() if path.is_dir() and (path / "task.toml").exists())


def select_tasks(task_dirs: list[Path], selectors: list[str] | None) -> list[Path]:
    if not selectors:
        return task_dirs
    requested = {item.strip() for selector in selectors for item in selector.split(",") if item.strip()}
    by_id = {task_id(path): path for path in task_dirs}
    unknown = sorted(requested - set(by_id))
    if unknown:
        available = ", ".join(sorted(by_id))
        raise TaskDefinitionError(f"unknown task(s): {', '.join(unknown)}; available: {available}")
    return [by_id[item] for item in sorted(requested)]


def read_reward(path: Path, default: float) -> float:
    if not path.exists():
        return default
    try:
        return float(path.read_text().strip())
    except ValueError:
        return default


def artifact_listing(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(str(item.relative_to(path)) for item in path.rglob("*") if item.is_file())


def run_task(task_dir: Path, *, run_root: Path, keep_workdir: bool = False) -> dict[str, Any]:
    data = validate_task_dir(task_dir)
    metadata = data["metadata"]
    current_task_id = str(metadata["task_id"])
    verifier = data.get("verifier", {})
    timeout = float(verifier.get("timeout_sec", 300))
    task_workdir = run_root / current_task_id
    artifacts = task_workdir / "artifacts"
    verifier_dir = task_workdir / "verifier"
    tri_home = task_workdir / "tri-home"
    task_workdir.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    verifier_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "TRI_BENCH_ARTIFACTS": str(artifacts),
            "TRI_BENCH_REPO_ROOT": str(ROOT),
            "TRI_BENCH_TASK_DIR": str(task_dir),
            "TRI_BENCH_VERIFIER_DIR": str(verifier_dir),
            "TRI_BENCH_WORKDIR": str(task_workdir),
            "TRI_HOME": str(tri_home),
            "PYTHONPATH": f"{ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep),
        }
    )

    started = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(
            ["bash", str(task_dir / "tests" / "test.sh")],
            cwd=task_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        proc = subprocess.CompletedProcess(
            exc.cmd,
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"timed out after {timeout} seconds",
        )
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    ok = proc.returncode == 0 and not timed_out
    reward = read_reward(verifier_dir / "reward.txt", 1.0 if ok else 0.0)
    return {
        "task_id": current_task_id,
        "ok": ok,
        "reward": reward,
        "timed_out": timed_out,
        "returncode": proc.returncode,
        "wall_ms": wall_ms,
        "metadata": metadata,
        "paths": {
            "task": str(task_dir),
            "workdir": str(task_workdir) if keep_workdir else None,
        },
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "artifacts": artifact_listing(artifacts),
    }


def markdown_summary(results: dict[str, Any]) -> str:
    lines = [
        "# Tri Task Benchmark",
        "",
        f"- Created: `{results['created_at']}`",
        f"- Tasks root: `{results['config']['tasks_root']}`",
        f"- Tasks: `{len(results['tasks'])}`",
        "",
        "| Task | Status | Reward | Wall ms | Category |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in results["tasks"]:
        status = "ok" if item["ok"] else ("timeout" if item["timed_out"] else "failed")
        category = item.get("metadata", {}).get("category", "")
        lines.append(f"| {item['task_id']} | {status} | {item['reward']:.3f} | {item['wall_ms']:.3f} | {category} |")
    passed = sum(1 for item in results["tasks"] if item["ok"])
    reward = sum(float(item.get("reward", 0)) for item in results["tasks"])
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            f"Passed `{passed}` of `{len(results['tasks'])}` task verifiers. Total reward: `{reward:.3f}`.",
            "",
            "These tasks are DeepSWE-style verifier packages for Tri-specific workflows. They complement the workflow benchmark by checking whether observable Tri behavior remains correct.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_results(out_dir: Path, results: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    (out_dir / "summary.md").write_text(markdown_summary(results))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run DeepSWE-style Tri benchmark task verifiers.")
    parser.add_argument("--tasks-root", type=Path, default=DEFAULT_TASKS_ROOT, help="Directory containing task packages.")
    parser.add_argument("--task", action="append", help="Task id to run. May be repeated or comma-separated.")
    parser.add_argument("--out", type=Path, default=None, help="Output directory for results.json and summary.md.")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep per-task working directories.")
    parser.add_argument("--list", action="store_true", help="List available tasks and exit.")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    tasks_root = args.tasks_root.resolve()
    try:
        task_dirs = discover_tasks(tasks_root)
        selected = select_tasks(task_dirs, args.task)
        for task_dir in selected:
            validate_task_dir(task_dir)
    except TaskDefinitionError as exc:
        parser.error(str(exc))

    if args.list:
        for task_dir in selected:
            data = load_task_toml(task_dir)
            metadata = data["metadata"]
            print(f"{metadata['task_id']}\t{metadata.get('display_title', '')}")
        return

    out_dir = args.out or Path("benchmark-results") / f"tasks-{time.strftime('%Y%m%d-%H%M%S')}"
    if args.keep_workdir:
        run_root = Path(tempfile.mkdtemp(prefix="tri-task-bench-"))
        cleanup = None
    else:
        temp = tempfile.TemporaryDirectory(prefix="tri-task-bench-")
        run_root = Path(temp.name)
        cleanup = temp

    try:
        results = {
            "schema": SCHEMA,
            "created_at": utcnow(),
            "config": {
                "tasks_root": str(tasks_root),
                "task_ids": [task_id(path) for path in selected],
                "keep_workdir": args.keep_workdir,
                "run_root": str(run_root) if args.keep_workdir else None,
            },
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "tri": shutil.which("tri"),
                "git": shutil.which("git"),
            },
            "tasks": [run_task(task_dir, run_root=run_root, keep_workdir=args.keep_workdir) for task_dir in selected],
        }
        write_results(out_dir, results)
        print(markdown_summary(results), end="")
        print(f"Results written to {out_dir}")
        if any(not item["ok"] for item in results["tasks"]):
            raise SystemExit(1)
    finally:
        if cleanup is not None:
            cleanup.cleanup()


if __name__ == "__main__":
    main()
