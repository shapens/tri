"""Fan-out disk benchmark: N parallel agent attempts that edit, install
dependencies, and run a check — Tri versus git worktrees.

What it measures is the thing worktrees get wrong: allocated disk at peak and at
the end, as attempts grow. On macOS each target runs on its own sparse APFS
disk image, so used space comes straight from the volume's space-used
attribute (what ``df`` reports) and counts shared clone blocks once. Elsewhere it falls back to summing
``st_blocks`` (which counts clones as full copies, overstating Tri).

Targets:
- ``tri``: API edits + ephemeral ``tri run`` checks; dependencies come from a
  shared Tri environment.
- ``tri-mount``: each attempt also keeps a persistent mounted directory (the
  closest analogue to a worktree), edited on disk and synced.
- ``git``: one ``git worktree`` per attempt, dependencies installed per worktree.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "tri.benchmark.fanout.v1"

INSTALL_SCRIPT = '''\
import os, sys
from pathlib import Path
lock = Path("deps.lock").read_text().split()
count, size = int(lock[0]), int(lock[1])
root = Path("node_modules")
for i in range(count):
    pkg = root / f"pkg{i // 50}"
    pkg.mkdir(parents=True, exist_ok=True)
    body = (f"// dependency {i} {lock[2]}\\n" * (size // 24 + 1))[:size]
    (pkg / f"mod{i}.js").write_text(body)
'''

CHECK_SCRIPT = '''\
import sys
from pathlib import Path
lock = Path("deps.lock").read_text().split()
count = int(lock[0])
found = sum(1 for _ in Path("node_modules").rglob("*.js"))
if found != count:
    sys.exit(f"expected {count} dependency files, found {found}")
attempt = Path("src/attempt.txt").read_text()
print("ok", attempt.strip(), found)
'''


# ---------------------------------------------------------------------------
# Measurement


class Volume:
    """A place to run one target, with a way to read allocated bytes."""

    def __init__(self, root: Path, measure: str, detach: Callable[[], None] | None = None) -> None:
        self.root = root
        self.measure = measure
        self._detach = detach
        self.baseline = 0
        self.baseline = self.settled()

    def used(self) -> int:
        if self.measure == "volume-used":
            # df reports the APFS volume's own space-used attribute, which counts
            # blocks shared by clones once. (statvfs free space on a sparse image
            # tracks the host disk's free space instead, so it is not usable.)
            out = subprocess.run(["df", "-k", str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.splitlines()
            return int(out[1].split()[2]) * 1024 - self.baseline
        return allocated_bytes(self.root)

    def settled(self, timeout: float = 8.0) -> int:
        """Read usage once the filesystem stops moving (APFS frees and writes
        metadata asynchronously for about a second after bursts)."""
        if self.measure != "volume-used":
            return self.used()
        os.sync()
        deadline = time.time() + timeout
        last = self.used()
        while time.time() < deadline:
            time.sleep(0.5)
            current = self.used()
            if abs(current - last) < 256 * 1024:
                return current
            last = current
        return last

    def close(self) -> None:
        if self._detach is not None:
            self._detach()


def allocated_bytes(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
            except OSError:
                pass
    return total


def make_volume(workdir: Path, name: str, mode: str) -> Volume:
    root = workdir / name
    if mode in ("auto", "apfs") and sys.platform == "darwin" and shutil.which("hdiutil"):
        image = workdir / f"{name}.sparseimage"
        root.mkdir(parents=True, exist_ok=True)
        create = subprocess.run(
            ["hdiutil", "create", "-quiet", "-size", "64g", "-type", "SPARSE", "-fs", "APFS", "-volname", f"tribench-{name}", str(image.with_suffix(""))],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if create.returncode == 0:
            attach = subprocess.run(
                ["hdiutil", "attach", "-quiet", "-nobrowse", "-noverify", "-noautoopen", "-mountpoint", str(root), str(image)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if attach.returncode == 0:
                # Keep Spotlight and FSEvents from writing their own data to the measured volume.
                (root / ".metadata_never_index").write_text("")
                (root / ".fseventsd").mkdir(exist_ok=True)
                (root / ".fseventsd" / "no_log").write_text("")
                subprocess.run(["mdutil", "-i", "off", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                def detach() -> None:
                    subprocess.run(["hdiutil", "detach", "-quiet", "-force", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    image.unlink(missing_ok=True)

                return Volume(root, "volume-used", detach)
        if mode == "apfs":
            raise RuntimeError(f"could not create APFS volume: {create.stderr}")
    root.mkdir(parents=True, exist_ok=True)
    return Volume(root, "st_blocks")


class PeakSampler:
    def __init__(self, volume: Volume, interval: float = 0.05) -> None:
        self.volume = volume
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval)

    def sample(self) -> int:
        try:
            used = self.volume.used()
        except OSError:
            return self.peak
        self.peak = max(self.peak, used)
        return used

    def __enter__(self) -> "PeakSampler":
        if self.volume.measure == "volume-used":
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        self.sample()


# ---------------------------------------------------------------------------
# Fixture


def make_fixture(repo: Path, *, source_files: int, file_size: int, dep_files: int, dep_file_size: int) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    body = ("x" * 63 + "\n") * (file_size // 64 + 1)
    for i in range(source_files):
        path = repo / "src" / f"mod{i // 100}" / f"file{i}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"file {i}\n" + body[:file_size])
    (repo / "src" / "attempt.txt").write_text("base\n")
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools" / "install.py").write_text(INSTALL_SCRIPT)
    (repo / "tools" / "check.py").write_text(CHECK_SCRIPT)
    (repo / "deps.lock").write_text(f"{dep_files} {dep_file_size} v1\n")
    (repo / ".gitignore").write_text("node_modules/\n__pycache__/\n")
    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "bench@example.com"], repo)
    run(["git", "config", "user.name", "Bench"], repo)
    run(["git", "add", "."], repo)
    run(["git", "commit", "-q", "-m", "fixture"], repo)


def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc


# ---------------------------------------------------------------------------
# Targets


def tri(env: dict[str, str], *argv: str) -> dict[str, Any]:
    proc = subprocess.run([sys.executable, "-m", "tri", *argv], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tri {' '.join(argv)} returned non-JSON: {proc.stderr[:2000]}") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"tri {' '.join(argv)} failed: {json.dumps(payload.get('diagnostics'))[:2000]}")
    return payload["data"]


class Project:
    """What each attempt works on: how to lay out the repo, install dependencies,
    edit, and check. ``synthetic`` generates a fixture; otherwise ``--repo``."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.real = args.repo is not None
        if self.real:
            self.install = args.install
            self.check = args.check
            self.key_files = args.key_file or []
            self.outputs = args.env_output or []
        else:
            self.install = f"{sys.executable} tools/install.py"
            self.check = f"{sys.executable} tools/check.py"
            self.key_files = ["deps.lock"]
            self.outputs = ["node_modules"]

    def create(self, repo: Path) -> None:
        if self.real:
            run(["git", "clone", "--quiet", "--depth", "1", Path(self.args.repo).resolve().as_uri(), str(repo)], Path.cwd())
            run(["git", "config", "user.email", "bench@example.com"], repo)
            run(["git", "config", "user.name", "Bench"], repo)
        else:
            make_fixture(repo, source_files=self.args.source_files, file_size=self.args.file_size, dep_files=self.args.dep_files, dep_file_size=self.args.dep_file_size)

    def edit_script(self, attempt: int) -> str:
        """Shell snippet that makes this attempt's edit (run inside the checkout)."""
        if self.real:
            text = self.args.edit_text.replace("{n}", str(attempt))
            return f"printf %s {shlex.quote(text)} >> {shlex.quote(self.args.edit_file)}"
        return (
            f"printf %s {shlex.quote(f'attempt {attempt}' + chr(10))} > src/attempt.txt && "
            f"printf %s {shlex.quote(f'edited by attempt {attempt}' + chr(10))} > src/mod0/file0.txt"
        )


def run_attempts(args: argparse.Namespace, sampler: "PeakSampler", attempt_fn: Callable[[int], bool], seconds: list[float]) -> int:
    """Run attempts with up to ``args.parallel`` at once; returns checks passed."""
    from concurrent.futures import ThreadPoolExecutor

    def one(attempt: int) -> bool:
        started = time.time()
        ok = attempt_fn(attempt)
        seconds.append(round(time.time() - started, 3))
        sampler.sample()
        return ok

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        return sum(1 for ok in pool.map(one, range(args.attempts)) if ok)


def bench_tri(volume: Volume, args: argparse.Namespace, *, mounts: bool, backend: str | None = None, env_link: bool = False) -> dict[str, Any]:
    project = Project(args)
    repo = volume.root / "repo"
    project.create(repo)
    env = os.environ.copy()
    env["TRI_HOME"] = str(volume.root / "tri-home")
    env.pop("TRI_ACTOR", None)
    env["TRI_AUTO_REAP"] = "0"
    if backend:
        env["TRI_MATERIALIZE_BACKEND"] = backend
    tri(env, "import", str(repo), "--name", "app")
    if project.outputs:
        define = ["env", "define", "app", "deps", "--install", project.install]
        for key in project.key_files:
            define += ["--key-file", key]
        for output in project.outputs:
            define += ["--output", output]
        tri(env, *define)
    backends: list[str] = []
    seconds: list[float] = []
    # Build the shared environment once up front so attempts measure reuse.
    started = time.time()
    tri(env, "run", tri(env, "workspace", "create", "app", "--from", "main")["id"], "--", "true")
    env_build_seconds = round(time.time() - started, 3)

    def attempt_fn(attempt: int) -> bool:
        ws = tri(env, "workspace", "create", "app", "--from", "main")["id"]
        if mounts:
            mount = volume.root / "mounts" / f"attempt{attempt}"
            tri(env, "workspace", "mount", ws, str(mount), *(["--env-link"] if env_link else []))
            run(["sh", "-c", project.edit_script(attempt)], mount)
            tri(env, "workspace", "sync", ws)
            command = project.check
        else:
            command = f"{project.edit_script(attempt)} && {project.check}"
        check = tri(env, "run", ws, "--", "sh", "-c", command)
        backends.append(check["materialized_directory"].get("backend", "unknown"))
        return check["status"] == "passed"

    with PeakSampler(volume) as sampler:
        passed = run_attempts(args, sampler, attempt_fn, seconds)
    return {"backends": sorted(set(backends)), "checks_passed": passed, "peak_allocated_bytes": sampler.peak, "attempt_seconds": seconds, "env_build_seconds": env_build_seconds}


def bench_git(volume: Volume, args: argparse.Namespace) -> dict[str, Any]:
    project = Project(args)
    repo = volume.root / "repo"
    project.create(repo)
    git_lock = threading.Lock()  # `git worktree add` and commits contend on the shared repo's locks
    seconds: list[float] = []

    def attempt_fn(attempt: int) -> bool:
        wt = volume.root / "worktrees" / f"attempt{attempt}"
        with git_lock:
            run(["git", "worktree", "add", "-q", "-b", f"attempt{attempt}", str(wt)], repo)
        check = subprocess.run(["sh", "-c", f"{project.install} && {project.edit_script(attempt)} && {project.check}"], cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with git_lock:
            run(["git", "commit", "-qam", f"attempt {attempt}"], wt)
        return check.returncode == 0

    with PeakSampler(volume) as sampler:
        passed = run_attempts(args, sampler, attempt_fn, seconds)
    return {"backends": ["worktree"], "checks_passed": passed, "peak_allocated_bytes": sampler.peak, "attempt_seconds": seconds}


TARGETS: dict[str, Callable[[Volume, argparse.Namespace], dict[str, Any]]] = {
    "tri": lambda volume, args: bench_tri(volume, args, mounts=False),
    "tri-mount": lambda volume, args: bench_tri(volume, args, mounts=True),
    "tri-mount-link": lambda volume, args: bench_tri(volume, args, mounts=True, env_link=True),
    "tri-copy": lambda volume, args: bench_tri(volume, args, mounts=False, backend="copy"),
    "git": bench_git,
}


def run_target(target: str, workdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    volume = make_volume(workdir, target, args.volume)
    started = time.time()
    try:
        data = TARGETS[target](volume, args)
        end = volume.settled()
        return {
            "target": target,
            "status": "ok",
            "measure": volume.measure,
            "wall_s": round(time.time() - started, 3),
            "end_allocated_bytes": end,
            "peak_allocated_bytes": max(data.pop("peak_allocated_bytes"), end),
            "per_attempt_bytes": end // max(args.attempts, 1),
            **data,
        }
    except Exception as exc:  # report and continue with other targets
        return {"target": target, "status": "failed", "measure": volume.measure, "error": str(exc), "wall_s": round(time.time() - started, 3), "end_allocated_bytes": 0, "peak_allocated_bytes": 0, "checks_passed": 0, "backends": [], "attempt_seconds": []}
    finally:
        volume.close()


def median(values: list[float]) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    return f"{ordered[len(ordered) // 2]:.1f}"


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    params = payload["parameters"]
    lines = [
        "# Fan-out disk benchmark",
        "",
        f"- Created: `{payload['created_at']}`",
        f"- Platform: `{payload['platform']}`",
        f"- Attempts: `{params['attempts']}` (`{params.get('parallel', 1)}` at a time)",
        (f"- Repository: `{params['repo']}`; install `{params['install']}`; check `{params['check']}`; env outputs `{params['env_output']}` keyed by `{params['key_file']}`" if params.get("repo") else f"- Synthetic fixture: `{params['source_files']}` source files x `{params['file_size']}` B; `{params['dep_files']}` dependency files x `{params['dep_file_size']}` B"),
        "",
        "| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Median attempt s | Checks | Backends |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in payload["results"]:
        lines.append(
            f"| {item['target']} | {item['status']} | {item['measure']} | {human(item['peak_allocated_bytes'])} | {human(item['end_allocated_bytes'])} | "
            f"{human(item.get('per_attempt_bytes', 0))} | {item['wall_s']} | {median(item.get('attempt_seconds', []))} | {item['checks_passed']} | {', '.join(item.get('backends', []))} |"
        )
        if item["status"] != "ok":
            lines.append(f"|  | error: {item.get('error', '')[:200]} |  |  |  |  |  |  |  |  |")
    lines += [
        "",
        "`volume-used` is the dedicated APFS volume's space-used figure (as `df` reports it), so blocks shared by clones count once. "
        "`st_blocks` sums per-file allocation and counts every clone as a full copy, which overstates Tri on clone backends.",
        "",
    ]
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tri-bench-fanout", description=__doc__.split("\n\n")[0])
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--source-files", type=int, default=2000)
    parser.add_argument("--file-size", type=int, default=1024)
    parser.add_argument("--dep-files", type=int, default=5000)
    parser.add_argument("--dep-file-size", type=int, default=2048)
    parser.add_argument("--targets", default="tri,tri-mount,git")
    parser.add_argument("--parallel", type=int, default=1, help="attempts to run at once")
    parser.add_argument("--repo", help="benchmark a real git repository (shallow-cloned from HEAD) instead of the synthetic fixture")
    parser.add_argument("--install", help="dependency install/build command (real repo)")
    parser.add_argument("--check", help="check command each attempt runs (real repo)")
    parser.add_argument("--key-file", action="append", help="files that key the shared environment (real repo)")
    parser.add_argument("--env-output", action="append", help="directories the install produces (real repo)")
    parser.add_argument("--edit-file", help="file each attempt appends to (real repo)")
    parser.add_argument("--edit-text", default="\n// tri-fanout attempt {n}\n", help="text appended by each attempt; {n} is the attempt number")
    parser.add_argument("--volume", choices=["auto", "apfs", "none"], default="auto")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.repo and not (args.install and args.check and args.edit_file):
        parser.error("--repo needs --install, --check and --edit-file")
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        parser.error(f"unknown targets: {unknown}; available: {sorted(TARGETS)}")
    output = args.output or ROOT / "benchmark-results" / f"fanout-{time.strftime('%Y%m%d-%H%M%S')}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tri-fanout-") as tmp:
        workdir = Path(tmp)
        results = [run_target(target, workdir, args) for target in targets]
    import platform

    payload = {
        "schema": SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "parameters": {k: getattr(args, k) for k in ("attempts", "parallel", "source_files", "file_size", "dep_files", "dep_file_size", "volume", "repo", "install", "check", "key_file", "env_output", "edit_file")},
        "results": results,
    }
    (output / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    write_summary(output / "summary.md", payload)
    print((output / "summary.md").read_text())


if __name__ == "__main__":
    main()
