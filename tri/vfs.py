"""Virtual checkouts: a whole-workspace view whose content is faulted in on first read.

A view has two layers:

- the *lower* layer is the manifest written at mount time: every path the
  mounting actor may read in the mounted snapshot (the same visibility filter
  ``files list`` applies; hidden and secret-like paths are never in it), with
  its blob hash, size, and mode, but no content. Directories are derived from
  the visible files, so a directory that holds only hidden files never
  appears.
- the *upper* layer is an ordinary directory holding every file the agent
  created or changed, at its view path (copy-up on first write), plus a list
  of whiteouts: view paths deleted since the mount.

Content for lower files comes from a ``Source``: the local Store, or a remote
``tri serve`` through the per-machine blob cache. Syncing reads only the
upper layer and the whiteouts, so it never faults anything in, and it works
whether or not the server is still running.

``python -m tri.vfs serve <state-dir>`` is the daemon a detached mount runs.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import signal
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from tri.fuse import Attr

STATE_VERSION = 1
STATS_FLUSH_SECONDS = 0.25
LOWER_CACHE_BYTES = 64 << 20
PREFETCH_BYTES = 256 << 10
PREFETCH_MAX_FILE = 64 << 10


def _atomic_write(path: Path, payload: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _mode_bits(entry: dict[str, Any], read_only: bool) -> int:
    mode = entry.get("mode", "100644")
    if mode == "120000":
        return stat.S_IFLNK | 0o777
    if mode == "100755":
        return stat.S_IFREG | (0o555 if read_only else 0o755)
    return stat.S_IFREG | (0o444 if read_only else 0o644)


class Stats:
    """Counters a view reports in JSON (files faulted, bytes fetched, ...)."""

    FIELDS = ("files_faulted", "dirs_faulted", "bytes_fetched", "cache_hits", "files_prefetched", "copy_ups", "lookups", "dirs_listed", "reads", "writes")

    def __init__(self, path: Path | None, base: dict[str, Any] | None = None) -> None:
        self.path = path
        self.values: dict[str, Any] = {name: 0 for name in self.FIELDS}
        if base:
            self.values.update(base)
        self.first_fault_at: float | None = None
        self.dirty = True
        self.last_flush = 0.0
        self.seq = 0

    def add(self, name: str, amount: int = 1) -> None:
        self.values[name] = self.values.get(name, 0) + amount
        self.dirty = True

    def flush(self, *, force: bool = False) -> None:
        if self.path is None or not self.dirty:
            return
        now = time.monotonic()
        if not force and now - self.last_flush < STATS_FLUSH_SECONDS:
            return
        self.last_flush = now
        self.dirty = False
        self.seq += 1
        try:
            _atomic_write(self.path, {**self.values, "seq": self.seq, "updated_at": time.time()})
        except OSError:
            self.dirty = True


def request_stats(state_dir: Path, timeout: float = 2.0) -> None:
    """Ask a live daemon (SIGUSR1) to write current counters, and wait for them."""
    if not hasattr(signal, "SIGUSR1") or not daemon_alive(state_dir):
        return
    pid = daemon_info(state_dir).get("pid")
    if not isinstance(pid, int):
        return
    stats_path = Path(state_dir) / "stats.json"

    def seq() -> Any:
        try:
            return json.loads(stats_path.read_text()).get("seq")
        except (OSError, ValueError):
            return None

    before = seq()
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and seq() == before:
        time.sleep(0.01)


def read_stats(state_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / "stats.json").read_text())
    except (OSError, ValueError):
        data = {}
    return {name: int(data.get(name, 0)) for name in Stats.FIELDS}


# -- content sources ----------------------------------------------------------


class LocalSource:
    """Blobs and lazy directory listings from the local Store (opened lazily, never packed)."""

    kind = "local"

    def __init__(self, home: str, *, workspace: str | None = None, snapshot: str | None = None, actor: str | None = None) -> None:
        self.home = home
        self.workspace = workspace
        self.snapshot = snapshot
        self.actor = actor or "actor_local"
        self._store: Any = None
        self._hidden: list[str] | None = None

    def read(self, path: str, entry: dict[str, Any], stats: Stats) -> bytes:
        self.prepare()
        data = self._store.read_blob(entry["blob"])
        self._store._blob_cache.pop(entry["blob"], None)  # the view keeps its own bounded cache
        stats.add("bytes_fetched", len(data))
        return data

    def list_dir(self, directory: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.prepare()
        from tri import cli

        if self._hidden is None:
            ref = self.workspace or self.snapshot
            assert ref is not None
            _, self._hidden = cli.resolve_ref_context(self._store, ref, actor=self.actor, action="workspace.read")
        assert self.snapshot is not None
        try:
            listing = cli.list_directory_visible(self._store, self.snapshot, directory, self._hidden)
        except cli.TriError as exc:
            if exc.diagnostic.get("code") in ("path_not_found", "policy_denied"):
                return [], []
            raise OSError(errno.EIO, exc.diagnostic.get("message") or "directory listing failed") from exc
        files = [
            {key: item[key] for key in ("name", "path", "blob", "hash", "size", "mode", "symlink_target") if item.get(key) is not None}
            for item in listing["files"]
        ]
        return listing["directories"], files

    def prepare(self) -> None:
        if self._store is None:
            os.environ["TRI_HOME"] = self.home  # Store() prefers TRI_HOME over its argument
            from tri.cli import Store

            self._store = Store(Path(self.home), pack_on_close=False)

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


class RemoteSource:
    """Blobs from ``tri serve`` through the per-machine cache (``TRI_CACHE_DIR``)."""

    kind = "remote"

    def __init__(self, url: str, snapshot: str) -> None:
        from tri.remote import RemoteClient, remote_token

        self.client = RemoteClient(url, remote_token(), timeout=120.0)
        self.snapshot = snapshot
        try:
            self.prefetch_files = max(0, int(os.environ.get("TRI_VIRTUAL_PREFETCH", "32")))
        except ValueError:
            self.prefetch_files = 32

    def prepare(self) -> None:
        pass

    def read(self, path: str, entry: dict[str, Any], stats: Stats, siblings: list[tuple[str, dict[str, Any]]] = ()) -> bytes:
        """One round trip fetches the file plus up to ``TRI_VIRTUAL_PREFETCH``
        (default 32) small uncached files from the same directory."""
        from tri.remote import cache_path, cache_put

        digest = entry["hash"]
        cached = cache_path(digest)
        try:
            data = cached.read_bytes()
            if hashlib.sha256(data).hexdigest() == digest:
                stats.add("cache_hits")
                return data
        except OSError:
            pass
        wanted = [path]
        budget = int(entry.get("size") or 0)
        for other, other_entry in siblings:
            if len(wanted) > self.prefetch_files:
                break
            size = int(other_entry.get("size") or 0)
            if other == path or size > PREFETCH_MAX_FILE or budget + size > PREFETCH_BYTES or cache_path(other_entry["hash"]).exists():
                continue
            wanted.append(other)
            budget += size
        fetched = self.client.command(["files", "fetch", self.snapshot, *sum((["--path", item] for item in wanted), []), "--max-bytes", str(max(budget, 1))])
        if not fetched.get("ok"):
            raise OSError(errno.EIO, f"fetch failed: {(fetched.get('diagnostics') or [{}])[0].get('code')}")
        expected = {path: digest, **{other: other_entry["hash"] for other, other_entry in siblings}}
        result: bytes | None = None
        for item in fetched["data"]["files"]:
            content = base64.b64decode(item["content"])
            if expected.get(item["path"]) != hashlib.sha256(content).hexdigest() or item.get("hash") != expected.get(item["path"]):
                if item["path"] == path:
                    raise OSError(errno.EIO, "content mismatch")
                continue
            cache_put(item["hash"], content)
            stats.add("bytes_fetched", len(content))
            if item["path"] == path:
                result = content
            else:
                stats.add("files_prefetched")
        if result is None:
            raise OSError(errno.EIO, "server deferred or omitted the file")
        return result

    def list_dir(self, directory: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        argv = ["files", "ls", self.snapshot, *([directory] if directory else [])]
        resp = self.client.command(argv)
        if not resp.get("ok"):
            code = (resp.get("diagnostics") or [{}])[0].get("code")
            if code in ("path_not_found", "policy_denied"):
                return [], []
            raise OSError(errno.EIO, f"list_dir failed: {code}")
        data = resp["data"]
        files = [
            {key: item[key] for key in ("name", "path", "blob", "hash", "size", "mode", "symlink_target") if item.get(key) is not None}
            for item in data["files"]
        ]
        return data["directories"], files

    def close(self) -> None:
        pass


def make_source(manifest: dict[str, Any]) -> Any:
    if manifest.get("source") == "remote":
        return RemoteSource(manifest["remote"], manifest["snapshot"])
    return LocalSource(
        manifest["home"],
        workspace=manifest.get("workspace"),
        snapshot=manifest.get("snapshot"),
        actor=manifest.get("actor"),
    )


# -- handles --------------------------------------------------------------------


class LowerHandle:
    """A read-only open of a lower file; content is faulted in on first read."""

    keep_cache = True

    def __init__(self, view: "VirtualView", path: str, entry: dict[str, Any]) -> None:
        self.view = view
        self.path = path
        self.entry = entry
        self.data: bytes | None = None

    def read(self, offset: int, size: int) -> bytes:
        if self.data is None:
            self.data = self.view.lower_content(self.path, self.entry)
        self.view.stats.add("reads")
        return self.data[offset : offset + size]

    def write(self, offset: int, data: Any) -> int:
        raise OSError(errno.EBADF, "opened read-only")

    def release(self) -> None:
        self.data = None


class UpperHandle:
    keep_cache = False

    def __init__(self, view: "VirtualView", fd: int) -> None:
        self.view = view
        self.fd = fd

    def read(self, offset: int, size: int) -> bytes:
        self.view.stats.add("reads")
        return os.pread(self.fd, size, offset)

    def write(self, offset: int, data: Any) -> int:
        self.view.stats.add("writes")
        return os.pwrite(self.fd, data, offset)

    def truncate(self, size: int) -> None:
        os.ftruncate(self.fd, size)

    def fstat(self) -> Attr:
        st = os.fstat(self.fd)
        return Attr(st.st_mode, st.st_size, st.st_mtime_ns, st.st_nlink, upper=True)

    def fsync(self) -> None:
        os.fsync(self.fd)

    def release(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


# -- the view ---------------------------------------------------------------------


class VirtualView:
    def __init__(self, state_dir: Path, manifest: dict[str, Any], source: Any, *, stats: Stats | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.upper = self.state_dir / "upper"
        self.upper.mkdir(parents=True, exist_ok=True)
        self.lower: dict[str, dict[str, Any]] = dict(manifest.get("entries", {}))
        self.read_only = bool(manifest.get("read_only"))
        self.mtime_ns = int(manifest.get("mounted_at", time.time()) * 1_000_000_000)
        self.source = source
        self.stats = stats or Stats(self.state_dir / "stats.json")
        self.lazy_trees = bool(manifest.get("lazy_trees"))
        if self.lazy_trees:
            raw_dirs = manifest.get("dirs") or {"": []}
            self.lower_dirs: dict[str, list[str]] = {path: sorted(names) for path, names in raw_dirs.items()}
            self._loaded_dirs: set[str] | None = set(self.lower_dirs.keys())
            self._known_dirs: set[str] | None = set(manifest.get("known_dirs") or [""]) | set(self.lower_dirs.keys())
        else:
            children: dict[str, set[str]] = {"": set()}
            for path in self.lower:
                parts = path.split("/")
                for depth in range(len(parts)):
                    parent = "/".join(parts[:depth])
                    name = parts[depth]
                    children.setdefault(parent, set()).add(name)
            self.lower_dirs = {path: sorted(names) for path, names in children.items()}
            self._loaded_dirs = None
            self._known_dirs = None
        state = load_layer_state(self.state_dir)
        self.whiteouts: set[str] = set(state["whiteouts"])
        self.whiteout_dirs: set[str] = set(state["whiteout_dirs"])
        self._lower_hashes: dict[str, str] = dict(state.get("lower_hashes", {}))
        self._cache: dict[str, bytes] = {}
        self._cache_bytes = 0
        self._faulted: set[str] = set()

    # -- lazy directory faulting ------------------------------------------------

    def _is_lower_dir(self, path: str) -> bool:
        if path in self.lower_dirs:
            return True
        return self._known_dirs is not None and path in self._known_dirs

    def _ensure_dir_loaded(self, directory: str) -> None:
        if self._loaded_dirs is None or directory in self._loaded_dirs:
            return
        if self._known_dirs is not None and directory not in self._known_dirs:
            return
        subdirs, files = self.source.list_dir(directory)
        names: list[str] = []
        for sub in subdirs:
            sub_path = sub["path"]
            if ".." in sub_path.split("/") or sub_path.startswith("/"):
                continue
            if self._known_dirs is not None:
                self._known_dirs.add(sub_path)
            names.append(sub["name"])
        for item in files:
            item_path = item["path"]
            if ".." in item_path.split("/") or item_path.startswith("/"):
                continue
            self.lower[item_path] = {key: item[key] for key in ("blob", "hash", "size", "mode", "symlink_target") if item.get(key) is not None}
            names.append(item["name"])
        self.lower_dirs[directory] = sorted(set(names))
        self._loaded_dirs.add(directory)
        self.stats.add("dirs_faulted")

    # -- layer state ------------------------------------------------------------

    def save(self) -> None:
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "whiteouts": sorted(self.whiteouts),
            "whiteout_dirs": sorted(self.whiteout_dirs),
        }
        if self._lower_hashes:
            payload["lower_hashes"] = dict(sorted(self._lower_hashes.items()))
        _atomic_write(self.state_dir / "layer.json", payload)

    def writable(self) -> None:
        if self.read_only:
            raise OSError(errno.EROFS, "read-only view")

    def file_count(self) -> int:
        return len(self.lower)

    # -- resolution -------------------------------------------------------------

    def _upper_lstat(self, path: str) -> os.stat_result | None:
        if not path:
            return None
        try:
            return os.lstat(self.upper / path)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def resolve(self, path: str) -> tuple[str, Any]:
        """('dir'|'upper'|'lower', detail) or raise ENOENT."""
        if path == "":
            return "dir", None
        st = self._upper_lstat(path)
        if st is not None:
            return ("dir" if stat.S_ISDIR(st.st_mode) else "upper"), st
        if path in self.whiteouts or path in self.whiteout_dirs:
            raise OSError(errno.ENOENT, path)
        parent = _parent(path)
        if parent:
            parent_kind, _ = self.resolve(parent)
            if parent_kind != "dir":
                raise OSError(errno.ENOTDIR, path)
        self._ensure_dir_loaded(parent)
        entry = self.lower.get(path)
        if entry is not None:
            return "lower", entry
        if self._is_lower_dir(path):
            return "dir", None
        raise OSError(errno.ENOENT, path)

    def getattr(self, path: str) -> Attr:
        self.stats.add("lookups")
        kind, detail = self.resolve(path)
        if kind == "lower":
            size = len((detail.get("symlink_target") or "").encode()) if detail.get("mode") == "120000" and detail.get("symlink_target") is not None else int(detail.get("size", 0))
            return Attr(_mode_bits(detail, self.read_only), size, self.mtime_ns)
        if kind == "upper":
            return Attr(detail.st_mode, detail.st_size, detail.st_mtime_ns, 1, upper=True)
        if detail is not None:
            mode = stat.S_IFDIR | (stat.S_IMODE(detail.st_mode) & (0o555 if self.read_only else 0o7777))
            return Attr(mode, 4096, detail.st_mtime_ns, 2, upper=True)
        return Attr(stat.S_IFDIR | (0o555 if self.read_only else 0o755), 4096, self.mtime_ns, 2)

    def listdir(self, path: str) -> list[str]:
        kind, _ = self.resolve(path)
        if kind != "dir":
            raise OSError(errno.ENOTDIR, path)
        self._ensure_dir_loaded(path)
        self.stats.add("dirs_listed")
        names: set[str] = set()
        upper_dir = self.upper / path if path else self.upper
        try:
            with os.scandir(upper_dir) as it:
                names.update(item.name for item in it)
        except (FileNotFoundError, NotADirectoryError):
            pass
        for name in self.lower_dirs.get(path, ()):
            if name in names:
                continue
            child = _join(path, name)
            if child in self.whiteouts or child in self.whiteout_dirs:
                continue
            names.add(name)
        return sorted(names)

    def readlink(self, path: str) -> str:
        kind, detail = self.resolve(path)
        if kind == "upper":
            if not stat.S_ISLNK(detail.st_mode):
                raise OSError(errno.EINVAL, path)
            return os.readlink(self.upper / path)
        if kind != "lower" or detail.get("mode") != "120000":
            raise OSError(errno.EINVAL, path)
        if detail.get("symlink_target") is not None:
            return detail["symlink_target"]
        return self.lower_content(path, detail).decode(errors="surrogateescape")

    # -- content ------------------------------------------------------------------

    def lower_content(self, path: str, entry: dict[str, Any]) -> bytes:
        digest = entry.get("hash") or entry.get("blob")
        cached = self._cache.get(digest)
        if cached is not None:
            return cached
        started = time.time()
        if getattr(self.source, "prefetch_files", 0):
            parent = _parent(path)
            siblings = [(other, self.lower[other]) for other in (_join(parent, name) for name in self.lower_dirs.get(parent, ())) if other in self.lower and other not in self.whiteouts]
            data = self.source.read(path, entry, self.stats, siblings)
        else:
            data = self.source.read(path, entry, self.stats)
        if digest not in self._faulted:
            self._faulted.add(digest)
            self.stats.add("files_faulted")
            if self.stats.first_fault_at is None:
                self.stats.first_fault_at = started
        if len(data) <= LOWER_CACHE_BYTES // 8:
            while self._cache and self._cache_bytes + len(data) > LOWER_CACHE_BYTES:
                _, dropped = self._cache.popitem()
                self._cache_bytes -= len(dropped)
            self._cache[digest] = data
            self._cache_bytes += len(data)
        return data

    def open(self, path: str, flags: int) -> Any:
        kind, detail = self.resolve(path)
        if kind == "dir":
            raise OSError(errno.EISDIR, path)
        wants_write = (flags & os.O_ACCMODE) != os.O_RDONLY or flags & os.O_TRUNC
        if wants_write:
            self.writable()
        if kind == "lower" and not wants_write:
            if detail.get("mode") == "120000":
                raise OSError(errno.ELOOP, path)
            return LowerHandle(self, path, detail)
        if kind == "lower":
            self.copy_up(path, empty=bool(flags & os.O_TRUNC))  # a truncating open never needs the old content
        fd = os.open(self.upper / path, flags & ~(os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_NOCTTY", 0)))
        return UpperHandle(self, fd)

    # -- copy-up and mutation -------------------------------------------------------

    def _ensure_upper_dir(self, path: str) -> None:
        if not path:
            return
        target = self.upper / path
        st = self._upper_lstat(path)
        if st is not None:
            if not stat.S_ISDIR(st.st_mode):
                raise OSError(errno.ENOTDIR, path)
            return
        self._ensure_upper_dir(_parent(path))
        os.mkdir(target, 0o755)

    def copy_up(self, path: str, *, empty: bool = False) -> None:
        entry = self.lower[path]
        self._ensure_upper_dir(_parent(path))
        target = self.upper / path
        if entry.get("mode") == "120000":
            os.symlink(self.readlink(path), target)
        else:
            data = b"" if empty else self.lower_content(path, entry)
            tmp = target.with_name(f".tri-copyup-{uuid.uuid4().hex}")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o755 if entry.get("mode") == "100755" else 0o644)
            os.replace(tmp, target)
        self.stats.add("copy_ups")
        if self.lazy_trees and entry.get("hash"):
            self._lower_hashes[path] = entry["hash"]
            self.save()

    def _parent_dir(self, path: str) -> None:
        kind, _ = self.resolve(_parent(path))
        if kind != "dir":
            raise OSError(errno.ENOTDIR, path)

    def _exists(self, path: str) -> str | None:
        try:
            return self.resolve(path)[0]
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return None
            raise

    def create(self, path: str, mode: int, flags: int) -> UpperHandle:
        self.writable()
        self._parent_dir(path)
        existing = self._exists(path)
        if existing is not None:
            if flags & os.O_EXCL:
                raise OSError(errno.EEXIST, path)
            return self.open(path, flags)
        self._ensure_upper_dir(_parent(path))
        fd = os.open(self.upper / path, os.O_CREAT | os.O_EXCL | (flags & os.O_ACCMODE if flags & os.O_ACCMODE else os.O_RDWR) | (flags & os.O_TRUNC), mode or 0o644)
        os.chmod(self.upper / path, mode & 0o777 or 0o644)
        if path in self.whiteouts:
            self.whiteouts.discard(path)
            self.save()
        return UpperHandle(self, fd)

    def mkdir(self, path: str, mode: int) -> None:
        self.writable()
        self._parent_dir(path)
        if self._exists(path) is not None:
            raise OSError(errno.EEXIST, path)
        self._ensure_upper_dir(_parent(path))
        os.mkdir(self.upper / path, mode or 0o755)
        if path in self.whiteout_dirs:
            self.whiteout_dirs.discard(path)
            self.save()

    def symlink(self, path: str, target: str) -> None:
        self.writable()
        self._parent_dir(path)
        if self._exists(path) is not None:
            raise OSError(errno.EEXIST, path)
        self._ensure_upper_dir(_parent(path))
        os.symlink(target, self.upper / path)
        if path in self.whiteouts:
            self.whiteouts.discard(path)
            self.save()

    def unlink(self, path: str) -> None:
        self.writable()
        kind, detail = self.resolve(path)
        if kind == "dir":
            raise OSError(errno.EISDIR, path)
        if kind == "upper":
            os.unlink(self.upper / path)
        elif self.lazy_trees and detail.get("hash"):
            self._lower_hashes[path] = detail["hash"]
        self.whiteouts.add(path)  # also covers files a previous sync recorded
        self.save()

    def rmdir(self, path: str) -> None:
        self.writable()
        if path == "":
            raise OSError(errno.EBUSY, path)
        kind, detail = self.resolve(path)
        if kind != "dir":
            raise OSError(errno.ENOTDIR, path)
        if self.listdir(path):
            raise OSError(errno.ENOTEMPTY, path)
        if detail is not None:
            os.rmdir(self.upper / path)
        if self._is_lower_dir(path):
            self.whiteout_dirs.add(path)
            self.save()

    def _view_tree(self, path: str) -> tuple[list[str], list[str]]:
        """(files, dirs) currently in the view at or under ``path``."""
        kind, _ = self.resolve(path)
        if kind != "dir":
            return [path], []
        files: list[str] = []
        dirs: list[str] = [path]
        for name in self.listdir(path):
            sub_files, sub_dirs = self._view_tree(_join(path, name))
            files.extend(sub_files)
            dirs.extend(sub_dirs)
        return files, dirs

    def _copy_up_tree(self, path: str) -> None:
        kind, _ = self.resolve(path)
        if kind == "lower":
            self.copy_up(path)
            return
        if kind == "upper":
            return
        self._ensure_upper_dir(path)
        for name in self.listdir(path):
            self._copy_up_tree(_join(path, name))

    def rename(self, old: str, new: str, *, noreplace: bool = False) -> None:
        self.writable()
        src_kind, _ = self.resolve(old)
        self._parent_dir(new)
        if new == old or new.startswith(old + "/"):
            raise OSError(errno.EINVAL, new)
        dst_kind = self._exists(new)
        if dst_kind is not None:
            if noreplace:
                raise OSError(errno.EEXIST, new)
            if src_kind == "dir" and dst_kind != "dir":
                raise OSError(errno.ENOTDIR, new)
            if src_kind != "dir" and dst_kind == "dir":
                raise OSError(errno.EISDIR, new)
            if dst_kind == "dir" and self.listdir(new):
                raise OSError(errno.ENOTEMPTY, new)
        moved_files, moved_dirs = self._view_tree(old)
        self._copy_up_tree(old)  # faults in what moves; nothing else
        if dst_kind == "dir":
            self.rmdir(new)
        elif dst_kind is not None:
            self.unlink(new)
        self._ensure_upper_dir(_parent(new))
        os.rename(self.upper / old, self.upper / new)
        for path in moved_files:
            if self.lazy_trees and path in self.lower and self.lower[path].get("hash"):
                self._lower_hashes[path] = self.lower[path]["hash"]
            self.whiteouts.add(path)
        for path in moved_dirs:
            if self._is_lower_dir(path):
                self.whiteout_dirs.add(path)
        for path in moved_files:
            self.whiteouts.discard(new + path[len(old) :])
        for path in moved_dirs:
            self.whiteout_dirs.discard(new + path[len(old) :])
        self.save()

    def truncate(self, path: str, size: int) -> None:
        self.writable()
        kind, _ = self.resolve(path)
        if kind == "dir":
            raise OSError(errno.EISDIR, path)
        if kind == "lower":
            self.copy_up(path, empty=size == 0)
        os.truncate(self.upper / path, size)

    def chmod(self, path: str, mode: int) -> None:
        self.writable()
        kind, detail = self.resolve(path)
        if kind == "lower":
            if (mode & 0o111 != 0) == (detail.get("mode") == "100755") or detail.get("mode") == "120000":
                return  # Tri records only the executable bit
            self.copy_up(path)
        if kind == "dir" and detail is None:
            self._ensure_upper_dir(path)
        os.chmod(self.upper / path, mode)

    def utimens(self, path: str, atime_ns: int | None, mtime_ns: int | None) -> None:
        self.writable()
        kind, detail = self.resolve(path)
        if kind == "lower" or (kind == "dir" and detail is None):
            return  # Tri does not record times; no copy-up for a touch
        st = os.lstat(self.upper / path)
        os.utime(self.upper / path, ns=(atime_ns if atime_ns is not None else st.st_atime_ns, mtime_ns if mtime_ns is not None else st.st_mtime_ns), follow_symlinks=False)


# -- state on disk ----------------------------------------------------------------


def load_layer_state(state_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / "layer.json").read_text())
    except (OSError, ValueError):
        data = {}
    return {
        "whiteouts": list(data.get("whiteouts", [])),
        "whiteout_dirs": list(data.get("whiteout_dirs", [])),
        "lower_hashes": dict(data.get("lower_hashes", {})),
    }


def load_manifest(state_dir: Path) -> dict[str, Any]:
    return json.loads((Path(state_dir) / "manifest.json").read_text())


def write_manifest(state_dir: Path, manifest: dict[str, Any]) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    _atomic_write(Path(state_dir) / "manifest.json", {"version": STATE_VERSION, **manifest})


def upper_changes(state_dir: Path) -> tuple[list[str], list[str]]:
    """(changed, deleted): every file in the upper layer, and whiteouts that are
    not shadowed by an upper file. Only the caller knows which are real changes
    (it compares content with the head)."""
    upper = Path(state_dir) / "upper"
    changed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(upper):
        rel_dir = Path(dirpath).relative_to(upper).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for name in list(dirnames):
            if os.path.islink(os.path.join(dirpath, name)):
                changed.append(prefix + name)
                dirnames.remove(name)
        for name in filenames:
            if name.startswith(".tri-copyup-"):
                continue
            changed.append(prefix + name)
    present = set(changed)
    layer = load_layer_state(state_dir)
    deleted = sorted(path for path in layer["whiteouts"] if path not in present)
    return sorted(changed), deleted


# -- daemon -----------------------------------------------------------------------


def daemon_info(state_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((Path(state_dir) / "daemon.json").read_text())
    except (OSError, ValueError):
        return {}


def daemon_alive(state_dir: Path) -> bool:
    """The daemon holds ``holder.lock`` for as long as it serves."""
    from tri.locking import FileLock

    lock_path = Path(state_dir) / "holder.lock"
    if not lock_path.exists():
        return False
    probe = FileLock(lock_path)
    try:
        return not probe.acquire(exclusive=True, blocking=False)
    finally:
        probe.close()


def serve(state_dir: Path) -> int:
    """Mount and serve one view until unmounted or signalled. Writes ``ready.json``
    once mounted (or with the error), keeps ``stats.json`` current, and holds
    ``holder.lock`` while alive."""
    from tri import fuse
    from tri.locking import FileLock

    state_dir = Path(state_dir)
    ready = state_dir / "ready.json"
    holder = FileLock(state_dir / "holder.lock")
    holder.acquire(exclusive=True)
    manifest = load_manifest(state_dir)
    mountpoint = Path(manifest["mountpoint"])
    stats = Stats(state_dir / "stats.json", base=read_stats(state_dir))
    source = make_source(manifest)
    source.prepare()  # import and open the store before reporting ready, not on the first read
    view = VirtualView(state_dir, manifest, source, stats=stats)
    mount = fuse.FuseMount(mountpoint, read_only=view.read_only, fsname=f"tri:{manifest.get('workspace', 'view')}")
    try:
        fd = mount.mount()
    except OSError as exc:
        _atomic_write(ready, {"ok": False, "error": str(exc), "pid": os.getpid()})
        holder.close()
        return 1
    server = fuse.FuseServer(fd, view, on_idle=lambda: stats.flush())
    fuse.install_stop_handlers(server, mount)

    def flush_now(signum: int, frame: Any) -> None:
        stats.dirty = True
        stats.flush(force=True)

    signal.signal(signal.SIGUSR1, flush_now)
    _atomic_write(state_dir / "daemon.json", {"pid": os.getpid(), "mountpoint": str(mountpoint), "backend": "fuse", "auto_unmount": mount.auto_unmount, "started_at": time.time()})
    stats.flush(force=True)
    _atomic_write(ready, {"ok": True, "pid": os.getpid(), "auto_unmount": mount.auto_unmount})
    try:
        server.serve()
    finally:
        stats.flush(force=True)
        if fuse.is_mounted(mountpoint):
            fuse.unmount(mountpoint, lazy=True)
        mount.unmount()
        source.close()
        holder.close()
    return 0


def serve_inline(state_dir: Path) -> int:
    """Foreground mode for ``--foreground``: same as the daemon, in this process."""
    return serve(state_dir)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "serve":
        return serve(Path(argv[1]))
    sys.stderr.write("usage: python -m tri.vfs serve <state-dir>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
