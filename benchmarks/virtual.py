"""Virtual checkout benchmark: time to first read and bytes fetched on a large tree.

One synthetic tree (``--files`` files in ``--packages`` packages) is imported
into a store. Each checkout mode then mounts a fresh workspace and reads one
file, through the local store and through a remote ``tri serve`` with an empty
per-machine cache:

- ``full``: ``tri workspace mount`` lays out every file;
- ``paths``: ``--paths <one package>`` lays out one package (the caller must
  know which one up front);
- ``virtual``: ``--virtual`` mounts the whole tree as a FUSE view and fetches
  content on first read (Linux only).

For each mode the benchmark records the mount time, the time from the start of
the mount to the first byte read, the bytes fetched (remote: bytes that crossed
the wire; local: bytes read from the store), and for ``virtual`` the cost of
walking one whole package afterwards. Every step is a ``tri`` subprocess, so
CLI start-up is counted, as an agent would pay it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "tri.benchmark.virtual.v1"


def generate(root: Path, files: int, packages: int, size: int) -> None:
    per = max(1, files // packages)
    count = 0
    for pkg in range(packages):
        folder = root / f"pkg{pkg:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(per):
            if count >= files:
                return
            line = f"# package {pkg} module {index}\n"
            (folder / f"mod{index:04d}.py").write_bytes((line * (size // len(line) + 1))[:size].encode())
            count += 1


def tri(env: dict[str, str], *argv: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "tri", *argv], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    elapsed = time.perf_counter() - started
    try:
        output = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"tri {' '.join(argv)} printed no JSON:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}") from None
    if not output.get("ok"):
        raise RuntimeError(f"tri {' '.join(argv)} failed: {json.dumps(output.get('diagnostics'))[:2000]}")
    return output, elapsed


def start_server(env: dict[str, str], tokens: Path) -> tuple[subprocess.Popen, str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "tri", "serve", "--port", str(port), "--quiet", "--auth-file", str(tokens)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with request.urlopen(f"{url}/v0/health", timeout=2):
                return proc, url
        except (URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("tri serve did not start")


def disk_files(path: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(path))


def measure(env: dict[str, str], mode: str, *, ws: str, dest: Path, target: str, package: str, remote: bool) -> dict[str, Any]:
    argv = ["workspace", "mount", ws, str(dest)]
    if mode == "paths":
        argv += ["--paths", package]
    elif mode == "virtual":
        argv += ["--virtual"]
    started = time.perf_counter()
    mounted, mount_s = tri(env, *argv)
    with open(dest / target, "rb") as handle:
        first = handle.read()
    first_read_s = time.perf_counter() - started
    result: dict[str, Any] = {"mode": mode, "transport": "remote" if remote else "local", "mount_s": round(mount_s, 3), "first_read_s": round(first_read_s, 3), "first_read_bytes": len(first)}
    data = mounted["data"]
    if mode == "virtual":
        synced, _ = tri(env, "workspace", "sync", ws)
        counters = synced["data"]["virtual"]["counters"]
        result.update(bytes_fetched=counters["bytes_fetched"], files_fetched=counters["files_faulted"], files_on_disk=0, view_files=data["virtual"]["counters"]["view_files"])
        walk_started = time.perf_counter()
        walked = 0
        for name in sorted(os.listdir(dest / package)):
            with open(dest / package / name, "rb") as handle:
                walked += len(handle.read())
        result["package_walk_s"] = round(time.perf_counter() - walk_started, 3)
        synced, _ = tri(env, "workspace", "sync", ws)
        after = synced["data"]["virtual"]["counters"]
        result.update(package_walk_bytes_fetched=after["bytes_fetched"], package_walk_files_fetched=after["files_faulted"])
        unmount_started = time.perf_counter()
        tri(env, "workspace", "unmount", ws)
        result["unmount_s"] = round(time.perf_counter() - unmount_started, 3)
    else:
        if remote:
            result.update(bytes_fetched=data["fetched_bytes"], files_fetched=data["files"] - data["cache_hits"])
        else:
            laid_out = disk_files(dest)
            result.update(bytes_fetched=sum((dest / p).stat().st_size for p in _walk(dest)), files_fetched=laid_out)
        result["files_on_disk"] = disk_files(dest)
        if not remote:
            tri(env, "leases", "reap", data["lease"], "--force")
        shutil.rmtree(dest, ignore_errors=True)
    return result


def _walk(root: Path) -> list[str]:
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--files", type=int, default=50000)
    parser.add_argument("--packages", type=int, default=200)
    parser.add_argument("--file-size", type=int, default=1024)
    parser.add_argument("--modes", default="full,paths,virtual")
    parser.add_argument("--transports", default="local,remote")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    modes = [mode for mode in args.modes.split(",") if mode]
    transports = [item for item in args.transports.split(",") if item]
    work = Path(tempfile.mkdtemp(prefix="tri-bench-virtual-"))
    results: list[dict[str, Any]] = []
    server = None
    try:
        source = work / "source"
        generate(source, args.files, args.packages, args.file_size)
        base_env = {key: value for key, value in os.environ.items() if not key.startswith("TRI_")}
        server_env = {**base_env, "TRI_HOME": str(work / "home"), "TRI_AUTO_REAP": "0"}
        _, import_s = tri(server_env, "import", str(source), "--name", "big", "--target", "main")
        package = f"pkg{args.packages // 2:03d}"
        target = f"{package}/mod0001.py"
        doctor, _ = tri(server_env, "doctor")
        if "local" in transports:
            for mode in modes:
                ws = tri(server_env, "workspace", "create", "big", "--from", "main")[0]["data"]["id"]
                results.append(measure(server_env, mode, ws=ws, dest=work / f"local-{mode}", target=target, package=package, remote=False))
                print(json.dumps(results[-1]), flush=True)
        if "remote" in transports:
            tokens = work / "tokens.json"
            token = tri(server_env, "auth", "issue", "agent:bench", "--file", str(tokens))[0]["data"]["token"]
            server, url = start_server(server_env, tokens)
            for mode in modes:
                client_env = {**base_env, "TRI_REMOTE": url, "TRI_TOKEN": token, "TRI_CACHE_DIR": str(work / f"cache-{mode}")}
                ws = tri(client_env, "workspace", "create", "big", "--from", "main")[0]["data"]["id"]
                results.append(measure(client_env, mode, ws=ws, dest=work / f"remote-{mode}", target=target, package=package, remote=True))
                print(json.dumps(results[-1]), flush=True)
        report = {
            "schema": SCHEMA,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "python": platform.python_version(),
            "fixture": {"files": args.files, "packages": args.packages, "file_size": args.file_size, "package": package, "read": target},
            "import_s": round(import_s, 3),
            "virtual_backend": doctor["data"].get("virtual_checkouts"),
            "results": results,
        }
        out_dir = args.output_dir or ROOT / "benchmark-results" / f"virtual-{time.strftime('%Y%m%d-%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        (out_dir / "summary.md").write_text(summary(report))
        print(summary(report))
        return 0
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(work, ignore_errors=True)


def _mib(value: int) -> str:
    return f"{value / (1 << 20):.2f} MiB" if value >= 1 << 20 else f"{value / 1024:.1f} KiB"


def summary(report: dict[str, Any]) -> str:
    fixture = report["fixture"]
    lines = [
        "# Virtual checkout benchmark",
        "",
        f"- Created: `{report['created_at']}`",
        f"- Platform: `{report['platform']}`, Python {report['python']}",
        f"- Fixture: `{fixture['files']}` files x `{fixture['file_size']}` B in `{fixture['packages']}` packages; first read `{fixture['read']}`; `--paths {fixture['package']}` for the partial checkout",
        f"- Import: {report['import_s']} s",
        "",
        "| Transport | Mode | Mount s | First read s | Bytes fetched | Files fetched | Files on disk | Package walk s | Walk bytes fetched |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["results"]:
        walk = f"{row['package_walk_s']}" if "package_walk_s" in row else "-"
        walk_bytes = _mib(row["package_walk_bytes_fetched"]) if "package_walk_bytes_fetched" in row else "-"
        lines.append(f"| {row['transport']} | {row['mode']} | {row['mount_s']} | {row['first_read_s']} | {_mib(row['bytes_fetched'])} | {row['files_fetched']} | {row['files_on_disk']} | {walk} | {walk_bytes} |")
    lines += [
        "",
        "First read is measured from the start of the mount command to the first byte of one file, so it includes CLI start-up and, for `virtual`, the daemon start. Remote bytes are content that crossed the wire into an empty per-machine cache; local bytes are content read from the store (for `full` and `paths`, the bytes written to disk). The package walk reads every file of one package through the view after the first read.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
