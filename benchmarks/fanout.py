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
    run(["git", "init", "-q"], repo)
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


def bench_tri(volume: Volume, args: argparse.Namespace, *, mounts: bool) -> dict[str, Any]:
    repo = volume.root / "repo"
    make_fixture(repo, source_files=args.source_files, file_size=args.file_size, dep_files=args.dep_files, dep_file_size=args.dep_file_size)
    env = os.environ.copy()
    env["TRI_HOME"] = str(volume.root / "tri-home")
    env.pop("TRI_ACTOR", None)
    env["TRI_AUTO_REAP"] = "0"
    tri(env, "import", str(repo), "--name", "app")
    tri(env, "env", "define", "app", "deps", "--key-file", "deps.lock", "--install", f"{sys.executable} tools/install.py", "--output", "node_modules")
    backends: list[str] = []
    passed = 0
    with PeakSampler(volume) as sampler:
        for attempt in range(args.attempts):
            ws = tri(env, "workspace", "create", "app", "--from", "main")["id"]
            if mounts:
                mount = volume.root / "mounts" / f"attempt{attempt}"
                tri(env, "workspace", "mount", ws, str(mount))
                (mount / "src" / "attempt.txt").write_text(f"attempt {attempt}\n")
                (mount / "src" / "mod0" / "file0.txt").write_text(f"edited by attempt {attempt}\n")
                tri(env, "workspace", "sync", ws)
            else:
                tri(env, "files", "write", ws, "src/attempt.txt", "--text", f"attempt {attempt}\n")
                tri(env, "files", "write", ws, "src/mod0/file0.txt", "--text", f"edited by attempt {attempt}\n")
            check = tri(env, "run", ws, "--", sys.executable, "tools/check.py")
            backends.append(check["materialized_directory"].get("backend", "unknown"))
            passed += 1 if check["status"] == "passed" else 0
            sampler.sample()
    return {"backends": sorted(set(backends)), "checks_passed": passed, "peak_allocated_bytes": sampler.peak}


def bench_git(volume: Volume, args: argparse.Namespace) -> dict[str, Any]:
    repo = volume.root / "repo"
    make_fixture(repo, source_files=args.source_files, file_size=args.file_size, dep_files=args.dep_files, dep_file_size=args.dep_file_size)
    passed = 0
    with PeakSampler(volume) as sampler:
        for attempt in range(args.attempts):
            wt = volume.root / "worktrees" / f"attempt{attempt}"
            run(["git", "worktree", "add", "-q", "-b", f"attempt{attempt}", str(wt)], repo)
            (wt / "src" / "attempt.txt").write_text(f"attempt {attempt}\n")
            (wt / "src" / "mod0" / "file0.txt").write_text(f"edited by attempt {attempt}\n")
            run([sys.executable, "tools/install.py"], wt)
            check = subprocess.run([sys.executable, "tools/check.py"], cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            passed += 1 if check.returncode == 0 else 0
            run(["git", "commit", "-qam", f"attempt {attempt}"], wt)
            sampler.sample()
    return {"backends": ["worktree"], "checks_passed": passed, "peak_allocated_bytes": sampler.peak}


TARGETS: dict[str, Callable[[Volume, argparse.Namespace], dict[str, Any]]] = {
    "tri": lambda volume, args: bench_tri(volume, args, mounts=False),
    "tri-mount": lambda volume, args: bench_tri(volume, args, mounts=True),
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
        return {"target": target, "status": "failed", "measure": volume.measure, "error": str(exc), "wall_s": round(time.time() - started, 3), "end_allocated_bytes": 0, "peak_allocated_bytes": 0, "checks_passed": 0, "backends": []}
    finally:
        volume.close()


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
        f"- Attempts: `{params['attempts']}`; source files: `{params['source_files']}` x `{params['file_size']}` B; dependency files: `{params['dep_files']}` x `{params['dep_file_size']}` B",
        "",
        "| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Checks | Backends |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in payload["results"]:
        lines.append(
            f"| {item['target']} | {item['status']} | {item['measure']} | {human(item['peak_allocated_bytes'])} | {human(item['end_allocated_bytes'])} | "
            f"{human(item.get('per_attempt_bytes', 0))} | {item['wall_s']} | {item['checks_passed']} | {', '.join(item.get('backends', []))} |"
        )
        if item["status"] != "ok":
            lines.append(f"|  | error: {item.get('error', '')[:200]} |  |  |  |  |  |  |  |")
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
    parser.add_argument("--volume", choices=["auto", "apfs", "none"], default="auto")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
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
        "parameters": {k: getattr(args, k) for k in ("attempts", "source_files", "file_size", "dep_files", "dep_file_size", "volume")},
        "results": results,
    }
    (output / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    write_summary(output / "summary.md", payload)
    print((output / "summary.md").read_text())


if __name__ == "__main__":
    main()
