"""Remote mode: one authoritative Tri service, every client disk a cache.

With `TRI_REMOTE=https://tri.example.com` (or `tri --remote <url> ...`) and a
bearer token in `TRI_TOKEN` or `TRI_TOKEN_FILE`, the CLI and `tri mcp` send
each command to the server's `/v0/command` instead of opening a local store.
The client keeps no metadata. Two commands need the client's disk and run
here:

- `tri workspace mount <ws> <dir> [--paths <glob>]` lists the workspace on the
  server, fetches only content missing from the per-machine blob cache
  (`TRI_CACHE_DIR`, default `~/.cache/tri`), and writes the files.
- `tri workspace sync <ws> [--path <dir>]` sends the files that changed since
  the mount back in one batch, guarded by the head the mount started from.

`files write --body <local file>` is converted to `--base64` so the server
never reads a client path.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import errno
import os
import socket
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

REMOTE_ENV = "TRI_REMOTE"
TOKEN_ENV = "TRI_TOKEN"
TOKEN_FILE_ENV = "TRI_TOKEN_FILE"
CACHE_ENV = "TRI_CACHE_DIR"
LOCAL_ONLY_COMMANDS = {"serve", "auth", "mcp", "cache", "storage"}
FETCH_CHUNK = 256
SKIP_DIRS = {".git"}


def remote_url() -> str | None:
    value = os.environ.get(REMOTE_ENV, "").strip()
    return value.rstrip("/") or None


def remote_token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    token_file = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if token_file:
        try:
            return Path(token_file).expanduser().read_text().strip() or None
        except OSError:
            return None
    return None


def cache_root() -> Path:
    value = os.environ.get(CACHE_ENV)
    return Path(value).expanduser() if value else Path.home() / ".cache" / "tri"


def error_envelope(argv: list[str], code: str, message: str, **fields: Any) -> dict[str, Any]:
    from tri.cli import envelope

    return envelope("tri.error", ["tri", *argv], ok=False, diagnostics=[{"code": code, "severity": "error", "message": message, "retryable": code == "remote_unreachable", **fields}])


_PREFERRED: dict[tuple[str, ...], int] = {}


def _never_reached_server(exc: BaseException) -> bool:
    """True when the request cannot have been processed, so another node may take it."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror)) or (isinstance(reason, OSError) and reason.errno in {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH})


class RemoteClient:
    """Talks to one Tri service. ``url`` may list several nodes (comma separated)
    serving the same store; a request moves to the next node only when the
    current one refused the connection or answered 503 (draining or not ready),
    so a write is never sent twice."""

    def __init__(self, url: str, token: str | None, *, timeout: float = 900.0) -> None:
        self.urls = [part.strip().rstrip("/") for part in url.split(",") if part.strip()] or [url.rstrip("/")]
        self.url = self.urls[_PREFERRED.get(tuple(self.urls), 0) % len(self.urls)]
        self.token = token
        self.timeout = timeout

    def _post_one(self, base: str, route: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        req = request.Request(f"{base}{route}", data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read()), False
        except HTTPError as exc:
            if exc.code == 503:
                return error_envelope(payload.get("argv", []), "remote_unreachable", "The Tri server is not ready (HTTP 503).", remote=base), True
            try:
                return json.loads(exc.read()), False
            except (ValueError, OSError):
                return error_envelope(payload.get("argv", []), "remote_unreachable", f"Remote returned HTTP {exc.code}.", remote=base), False
        except (URLError, OSError, TimeoutError) as exc:
            return error_envelope(payload.get("argv", []), "remote_unreachable", f"Cannot reach the Tri server: {exc}", remote=base), _never_reached_server(exc)

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = tuple(self.urls)
        first = _PREFERRED.get(key, 0) % len(self.urls)
        output: dict[str, Any] | None = None
        for offset in range(len(self.urls)):
            index = (first + offset) % len(self.urls)
            output, try_next = self._post_one(self.urls[index], route, payload)
            if not try_next:
                _PREFERRED[key] = index
                self.url = self.urls[index]
                return output
        assert output is not None
        if len(self.urls) > 1:
            output.setdefault("diagnostics", [{}])[0]["remotes_tried"] = list(self.urls)
        return output

    def command(self, argv: list[str]) -> dict[str, Any]:
        return self._post("/v0/command", {"argv": argv})

    def batch(self, commands: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post("/v0/batch", {"commands": commands})


# -- blob cache -----------------------------------------------------------------


def cache_path(digest: str) -> Path:
    return cache_root() / "blobs" / digest[:2] / digest


def cache_put(digest: str, data: bytes) -> Path:
    path = cache_path(digest)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def mount_state_path(dest: Path) -> Path:
    key = hashlib.sha256(str(dest.resolve()).encode()).hexdigest()[:24]
    return cache_root() / "mounts" / f"{key}.json"


def matches(path: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("/") + "/") for pattern in patterns)


def stat_key(path: Path) -> list[int]:
    st = os.lstat(path)
    return [st.st_size, st.st_mtime_ns]


# -- mount and sync -----------------------------------------------------------------


def remote_mount(client: RemoteClient, argv: list[str], workspace: str, dest: Path, paths: list[str]) -> dict[str, Any]:
    from tri.cli import envelope

    started = time.time()
    dest = dest.expanduser()
    if dest.exists() and any(dest.iterdir()):
        return error_envelope(argv, "mount_path_in_use", "Remote mounts need an empty or new directory.", object=str(dest))
    listing = client.command(["files", "list", workspace])
    if not listing.get("ok"):
        return listing
    snapshot = listing["data"]["snapshot"]
    wanted = [item for item in listing["data"]["files"] if matches(item["path"], paths)]
    by_path = {item["path"]: item for item in wanted}
    missing = [item["path"] for item in wanted if not cache_path(item["hash"]).exists()]
    fetched_bytes = 0
    pending = list(missing)
    while pending:
        chunk, pending = pending[:FETCH_CHUNK], pending[FETCH_CHUNK:]
        fetched = client.command(["files", "fetch", snapshot, *sum((["--path", path] for path in chunk), [])])
        if not fetched.get("ok"):
            return fetched
        for item in fetched["data"]["files"]:
            data = base64.b64decode(item["content"])
            if hashlib.sha256(data).hexdigest() != item["hash"]:
                return error_envelope(argv, "content_mismatch", "Fetched content does not match its hash.", object=item["path"])
            cache_put(item["hash"], data)
            fetched_bytes += len(data)
        pending = fetched["data"].get("deferred", []) + pending
    dest.mkdir(parents=True, exist_ok=True)
    state_entries: dict[str, Any] = {}
    root = dest.resolve()
    for path, item in sorted(by_path.items()):
        target = dest / path
        if ".." in Path(path).parts or Path(path).is_absolute() or not str(target.resolve()).startswith(str(root) + os.sep):
            return error_envelope(argv, "path_escaped_mount", "Server listed a path outside the mount.", object=path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = cache_path(item["hash"])
        mode = item.get("mode", "100644")
        if mode == "120000":
            os.symlink(source.read_text(), target)
        else:
            shutil.copyfile(source, target)
            os.chmod(target, 0o755 if mode == "100755" else 0o644)
        state_entries[path] = {"hash": item["hash"], "mode": mode, "stat": stat_key(target)}
    state = {"remote": client.url, "workspace": workspace, "snapshot": snapshot, "path": str(dest.resolve()), "paths": paths, "entries": state_entries}
    state_file = mount_state_path(dest)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))
    data = {
        "workspace": workspace,
        "path": str(dest.resolve()),
        "snapshot": snapshot,
        "remote": client.url,
        "files": len(state_entries),
        "fetched_bytes": fetched_bytes,
        "cache_hits": len(wanted) - len(missing),
        "sparse_paths": paths,
    }
    return envelope("tri.remote.mount", ["tri", *argv], start=started, data=data)


def remote_sync(client: RemoteClient, argv: list[str], workspace: str, dest: Path | None) -> dict[str, Any]:
    from tri.cli import envelope

    started = time.time()
    state_file = None
    state: dict[str, Any] | None = None
    if dest is not None:
        state_file = mount_state_path(dest)
    else:
        mounts = cache_root() / "mounts"
        for candidate in sorted(mounts.glob("*.json")) if mounts.is_dir() else []:
            loaded = json.loads(candidate.read_text())
            if loaded.get("workspace") == workspace and loaded.get("remote") == client.url:
                state_file, state = candidate, loaded
                break
    if state_file is None or not state_file.exists():
        return error_envelope(argv, "path_not_found", "No remote mount of this workspace on this machine.", object=workspace)
    state = state or json.loads(state_file.read_text())
    root = Path(state["path"])
    entries: dict[str, Any] = state["entries"]
    writes: dict[str, tuple[bytes, str]] = {}
    deletes: list[str] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            seen.add(rel)
            known = entries.get(rel)
            if known is not None and known["stat"] == stat_key(full):
                continue
            data = os.readlink(full).encode() if full.is_symlink() else full.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if known is not None and known["hash"] == digest:
                known["stat"] = stat_key(full)
                continue
            writes[rel] = (data, digest)
    for rel in entries:
        if rel not in seen:
            deletes.append(rel)
    commands: list[dict[str, Any]] = []
    for index, (rel, (data, _)) in enumerate(sorted(writes.items())):
        argv_write = ["files", "write", workspace, rel, "--base64", base64.b64encode(data).decode()]
        if index == 0:
            argv_write += ["--expect-head", state["snapshot"]]
        commands.append({"argv": argv_write})
    for index, rel in enumerate(sorted(deletes)):
        argv_delete = ["files", "delete", workspace, rel]
        if not commands and index == 0:
            argv_delete += ["--expect-head", state["snapshot"]]
        commands.append({"argv": argv_delete})
    snapshot = state["snapshot"]
    if commands:
        result = client.batch(commands)
        if not result.get("ok"):
            diagnostics = result.get("diagnostics") or [diag for item in result.get("data", {}).get("results", []) for diag in item.get("diagnostics", [])]
            stale = any(diag.get("code") == "stale_workspace_head" for diag in diagnostics)
            return envelope(
                "tri.error",
                ["tri", *argv],
                ok=False,
                diagnostics=[
                    {
                        "code": "stale_materialization" if stale else (diagnostics[0]["code"] if diagnostics else "remote_unreachable"),
                        "severity": "error",
                        "message": "The workspace moved on the server since this mount; remount to pick up its changes." if stale else "Remote sync failed.",
                        "retryable": False,
                        "details": diagnostics[:5],
                    }
                ],
                data={"workspace": workspace, "executed": result.get("data", {}).get("executed")},
            )
        for item in result["data"]["results"]:
            snapshot = item["data"].get("snapshot", snapshot)
    for rel, (_, digest) in writes.items():
        full = root / rel
        entries[rel] = {"hash": digest, "mode": entries.get(rel, {}).get("mode", "100644"), "stat": stat_key(full)}
    for rel in deletes:
        entries.pop(rel, None)
    state["snapshot"] = snapshot
    state_file.write_text(json.dumps(state))
    data = {"workspace": workspace, "path": str(root), "snapshot": snapshot, "remote": client.url, "written": sorted(writes), "deleted": sorted(deletes)}
    return envelope("tri.remote.sync", ["tri", *argv], start=started, data=data)


# -- runner worker ------------------------------------------------------------------


def execute_job(client: RemoteClient, job: dict[str, Any], *, sandbox_mode: str | None = None) -> dict[str, Any]:
    """Lay out a claimed job's files from the machine cache, run it sandboxed, and diff."""
    import tempfile

    from tri import sandbox

    root = Path(tempfile.mkdtemp(prefix="tri-runner-"))
    work = root / "work"
    work.mkdir()
    try:
        manifest = {item["path"]: item for item in job["files"]}
        pending = [path for path, item in manifest.items() if not cache_path(item["hash"]).exists()]
        while pending:
            chunk, pending = pending[:FETCH_CHUNK], pending[FETCH_CHUNK:]
            fetched = client.command(["runner", "fetch", job["id"], *sum((["--path", path] for path in chunk), [])])
            if not fetched.get("ok"):
                raise RuntimeError(f"fetch failed: {fetched.get('diagnostics')}")
            for item in fetched["data"]["files"]:
                data = base64.b64decode(item["content"])
                if hashlib.sha256(data).hexdigest() != item["hash"]:
                    raise RuntimeError(f"content mismatch for {item['path']}")
                cache_put(item["hash"], data)
            pending = fetched["data"].get("deferred", []) + pending
        for path, item in manifest.items():
            if ".." in Path(path).parts or Path(path).is_absolute():
                raise RuntimeError(f"unsafe path {path}")
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.get("mode") == "120000":
                os.symlink(cache_path(item["hash"]).read_text(), target)
            else:
                shutil.copyfile(cache_path(item["hash"]), target)
                os.chmod(target, 0o755 if item.get("mode") == "100755" else 0o644)
        mode = sandbox_mode or os.environ.get(sandbox.MODE_ENV) or "auto"
        hidden = [cache_root()]
        token_file = os.environ.get(TOKEN_FILE_ENV)
        if token_file:
            hidden.append(Path(token_file).expanduser())
        box = sandbox.build(mode, run_dir=root, hidden=hidden, limits=sandbox.Limits.from_env(timeout_seconds=job.get("timeout_seconds")))
        env = sandbox.scrubbed_env(root)  # never hand the runner's token to the job, even unsandboxed
        started = time.time()
        completed = sandbox.run(box, list(job["command"]), cwd=work, env=env)
        duration_ms = int((time.time() - started) * 1000)
        written: list[dict[str, Any]] = []
        seen: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(work):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(work).as_posix()
                seen.add(rel)
                data = os.readlink(full).encode() if full.is_symlink() else full.read_bytes()
                known = manifest.get(rel)
                if known is not None and known["hash"] == hashlib.sha256(data).hexdigest():
                    continue
                mode_bits = "120000" if full.is_symlink() else ("100755" if os.access(full, os.X_OK) else "100644")
                written.append({"path": rel, "mode": mode_bits, "content": base64.b64encode(data).decode()})
        deleted = sorted(path for path in manifest if path not in seen)
        return {
            "exit_status": completed.returncode,
            "stdout": base64.b64encode(completed.stdout).decode(),
            "stderr": base64.b64encode(completed.stderr).decode(),
            "duration_ms": duration_ms,
            "written": written,
            "deleted": deleted,
            "sandbox": ({**box.describe(), "timed_out": completed.timed_out} if box else {"kind": "off", "isolated": False}),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_remote_jobs(client: RemoteClient, *, max_jobs: int | None = None, idle_exit: bool = False, poll_seconds: float = 5.0, sandbox_mode: str | None = None) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    while True:
        claim = client.command(["runner", "claim", "--wait", str(poll_seconds)])
        if not claim.get("ok"):
            return {"jobs": jobs, "count": len(jobs), "error": (claim.get("diagnostics") or [{}])[0]}
        job = claim["data"]["job"]
        if job is None:
            if idle_exit:
                break
            continue
        try:
            result = execute_job(client, job, sandbox_mode=sandbox_mode)
        except Exception as exc:  # report the failure as a failed check instead of abandoning the claim
            result = {"exit_status": 125, "stdout": "", "stderr": base64.b64encode(f"[tri runner] {type(exc).__name__}: {exc}\n".encode()).decode(), "duration_ms": 0, "written": [], "deleted": []}
        done = client.command(["runner", "complete", job["id"], "--result", json.dumps(result)])
        jobs.append({"job": job["id"], "ok": bool(done.get("ok")), "check": (done.get("data") or {}).get("id"), "status": (done.get("data") or {}).get("status")})
        if max_jobs and len(jobs) >= max_jobs:
            break
    return {"jobs": jobs, "count": len(jobs)}


# -- dispatch -------------------------------------------------------------------


def _flag_values(argv: list[str], flag: str) -> list[str]:
    values = []
    for index, part in enumerate(argv):
        if part == flag and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif part.startswith(flag + "="):
            values.append(part.split("=", 1)[1])
    return values


def _without_flag(argv: list[str], flag: str) -> list[str]:
    out: list[str] = []
    skip = False
    for part in argv:
        if skip:
            skip = False
            continue
        if part == flag:
            skip = True
            continue
        if part.startswith(flag + "="):
            continue
        out.append(part)
    return out


def command_words(argv: list[str]) -> list[str]:
    """Positional words of argv after global flags (--actor, --response)."""
    words: list[str] = []
    skip = False
    for part in argv:
        if skip:
            skip = False
            continue
        if part in ("--actor", "--response", "--remote"):
            skip = True
            continue
        if part.startswith("--"):
            if words:
                break
            continue
        words.append(part)
        if len(words) >= 2:
            break
    return words


def dispatch(argv: list[str], url: str) -> dict[str, Any]:
    client = RemoteClient(url, remote_token())
    words = command_words(argv)
    if words[:2] == ["workspace", "mount"]:
        positional = [part for part in _without_flag(_without_flag(argv, "--paths"), "--response") if not part.startswith("--")]
        index = positional.index("mount")
        if len(positional) < index + 3:
            return error_envelope(argv, "invalid_command", "usage: tri workspace mount <workspace> <path> [--paths <glob>]")
        return remote_mount(client, argv, positional[index + 1], Path(positional[index + 2]), _flag_values(argv, "--paths"))
    if words[:2] == ["workspace", "sync"]:
        positional = [part for part in _without_flag(argv, "--path") if not part.startswith("--")]
        index = positional.index("sync")
        if len(positional) < index + 2:
            return error_envelope(argv, "invalid_command", "usage: tri workspace sync <workspace> [--path <dir>]")
        paths = _flag_values(argv, "--path")
        return remote_sync(client, argv, positional[index + 1], Path(paths[0]) if paths else None)
    if words[:2] == ["runner", "work"]:
        from tri.cli import envelope

        started = time.time()
        max_jobs = _flag_values(argv, "--max-jobs")
        interval = _flag_values(argv, "--interval")
        modes = _flag_values(argv, "--sandbox")
        summary = run_remote_jobs(client, max_jobs=int(max_jobs[0]) if max_jobs else None, idle_exit="--once" in argv, poll_seconds=float(interval[0]) if interval else 5.0, sandbox_mode=modes[0] if modes else None)
        ok = "error" not in summary
        return envelope("tri.runner.work", ["tri", *argv], ok=ok, start=started, data=summary, diagnostics=[summary["error"]] if not ok and summary["error"].get("code") else [])
    if words[:2] == ["files", "write"] and _flag_values(argv, "--body"):
        local = Path(_flag_values(argv, "--body")[0]).expanduser()
        try:
            content = local.read_bytes()
        except OSError as exc:
            return error_envelope(argv, "path_not_found", f"Cannot read {local}: {exc}", object=str(local))
        argv = _without_flag(argv, "--body") + ["--base64", base64.b64encode(content).decode()]
    return client.command(argv)
