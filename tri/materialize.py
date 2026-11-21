"""Materialize Tri trees into real directories as cheaply as the filesystem allows.

Backends:
- ``clone``: block-level copy-on-write clones from a raw checkout cache
  (APFS ``clonefile``, Linux ``FICLONE`` on btrfs/XFS/bcachefs/ZFS, Windows
  ReFS/Dev Drive ``FSCTL_DUPLICATE_EXTENTS_TO_FILE``). A clone costs metadata
  until one side writes. On macOS a whole base tree is cloned in one call.
- ``copy``: plain file writes. Works everywhere; reported as ``degraded``.

Every materialization records a stat baseline so later scans only read files
whose ``(size, mtime, inode, mode)`` changed. All regular files get the fixed
mtime ``FIXED_MTIME`` so any write is visible even on coarse-timestamp
filesystems.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

FIXED_MTIME = 946684800  # 2000-01-01T00:00:00Z: old enough to be stable, new enough for zip tools.
_FIXED_NS = FIXED_MTIME * 1_000_000_000
BACKEND_ENV = "TRI_MATERIALIZE_BACKEND"
RACY_WINDOW_NS = 2_000_000_000


class MaterializeError(Exception):
    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields


# ---------------------------------------------------------------------------
# Platform clone primitives


_clonefile_fn: Any = None
if sys.platform == "darwin":  # pragma: no branch
    try:
        import ctypes

        _libc = ctypes.CDLL(None, use_errno=True)
        _clonefile_fn = _libc.clonefile
        _clonefile_fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        _clonefile_fn.restype = ctypes.c_int
    except (OSError, AttributeError):  # pragma: no cover
        _clonefile_fn = None

_CLONE_NOFOLLOW = 0x0001
_CLONE_NOOWNERCOPY = 0x0002
_FICLONE = 0x40049409
_FSCTL_DUPLICATE_EXTENTS_TO_FILE = 0x00098344


def _clonefile(src: Path, dst: Path) -> None:
    import ctypes

    rc = _clonefile_fn(os.fsencode(src), os.fsencode(dst), _CLONE_NOFOLLOW | _CLONE_NOOWNERCOPY)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dst))


def _ficlone(src: Path, dst: Path) -> None:
    import fcntl

    with open(src, "rb") as fsrc:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            fcntl.ioctl(fd, _FICLONE, fsrc.fileno())
        except OSError:
            os.close(fd)
            try:
                os.unlink(dst)
            except OSError:
                pass
            raise
        os.close(fd)


def _refs_clone(src: Path, dst: Path) -> None:  # pragma: no cover - Windows only
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class DUPLICATE_EXTENTS_DATA(ctypes.Structure):
        _fields_ = [
            ("FileHandle", wintypes.HANDLE),
            ("SourceFileOffset", ctypes.c_longlong),
            ("TargetFileOffset", ctypes.c_longlong),
            ("ByteCount", ctypes.c_longlong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = src.stat().st_size
    with open(src, "rb") as fsrc, open(dst, "xb") as fdst:
        if size == 0:
            return
        free = wintypes.DWORD()
        sectors = wintypes.DWORD()
        bytes_per_sector = wintypes.DWORD()
        clusters = wintypes.DWORD()
        drive = os.path.splitdrive(os.path.abspath(dst))[0] + "\\"
        if not kernel32.GetDiskFreeSpaceW(drive, ctypes.byref(sectors), ctypes.byref(bytes_per_sector), ctypes.byref(free), ctypes.byref(clusters)):
            raise OSError(ctypes.get_last_error(), "GetDiskFreeSpaceW failed", str(dst))
        cluster = sectors.value * bytes_per_sector.value or 4096
        fdst.truncate(size)
        data = DUPLICATE_EXTENTS_DATA()
        data.FileHandle = msvcrt.get_osfhandle(fsrc.fileno())
        data.SourceFileOffset = 0
        data.TargetFileOffset = 0
        data.ByteCount = (size + cluster - 1) // cluster * cluster
        returned = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(fdst.fileno())),
            _FSCTL_DUPLICATE_EXTENTS_TO_FILE,
            ctypes.byref(data),
            ctypes.sizeof(data),
            None,
            0,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "FSCTL_DUPLICATE_EXTENTS_TO_FILE failed", str(dst))
        fdst.truncate(size)


def platform_clone_method() -> str | None:
    if sys.platform == "darwin" and _clonefile_fn is not None:
        return "clonefile"
    if sys.platform.startswith("linux"):
        return "ficlone"
    if os.name == "nt":  # pragma: no cover
        return "refs"
    return None


def clone_file(src: Path, dst: Path, method: str) -> None:
    """Clone ``src`` to a new file ``dst`` sharing blocks; raises OSError when unsupported."""
    if method == "clonefile":
        _clonefile(src, dst)
    elif method == "ficlone":
        _ficlone(src, dst)
    elif method == "refs":  # pragma: no cover
        _refs_clone(src, dst)
    else:
        raise OSError(errno.ENOTSUP, f"unknown clone method {method}", str(dst))


def clone_dir_supported() -> bool:
    return sys.platform == "darwin" and _clonefile_fn is not None


def clone_dir(src: Path, dst: Path) -> bool:
    """Clone a whole directory tree in one call (macOS only). ``dst`` must not exist."""
    if not clone_dir_supported():
        return False
    try:
        _clonefile(src, dst)
        return True
    except OSError:
        return False


_probe_cache: dict[int, str | None] = {}


def probe_clone(directory: Path) -> str | None:
    """Return the clone method that works on ``directory``'s volume, or None."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        dev = directory.stat().st_dev
    except OSError:
        return None
    if dev in _probe_cache:
        return _probe_cache[dev]
    method = platform_clone_method()
    result: str | None = None
    if method is not None:
        probe_dir = directory / f".tri-clone-probe-{uuid.uuid4().hex}"
        try:
            probe_dir.mkdir()
            src = probe_dir / "src"
            src.write_bytes(b"tri clone probe\n" * 64)
            clone_file(src, probe_dir / "dst", method)
            if (probe_dir / "dst").read_bytes() == src.read_bytes():
                result = method
        except OSError:
            result = None
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
    _probe_cache[dev] = result
    return result


# ---------------------------------------------------------------------------
# Helpers


def make_tree_writable(root: Path) -> None:
    _force_writable(root)


def _force_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                try:
                    st = os.lstat(p)
                    os.chmod(p, stat.S_IMODE(st.st_mode) | 0o600)
                except OSError:
                    pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def remove_tree(root: Path) -> None:
    """Delete a directory tree even when parts of it were made read-only."""
    root = Path(root)
    if root.is_symlink() or root.is_file():
        root.unlink()
        return
    if not root.exists():
        return

    def onerror(func: Callable[..., Any], path: str, exc_info: Any) -> None:
        # Unlock what blocked us; the pass below retries the whole tree. Never
        # re-invoke ``func``: for os.open it needs more than a path.
        for target in (os.path.dirname(path), path):
            try:
                if not os.path.islink(target):
                    os.chmod(target, 0o700)
            except OSError:
                pass

    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    shutil.rmtree(root, onerror=onerror)
    if root.exists():
        _force_writable(root)
        shutil.rmtree(root, onerror=onerror)
    if root.exists():
        _force_writable(root)
        shutil.rmtree(root, ignore_errors=True)


def symlink_target_escapes(root: Path, link_path: Path, target: str) -> bool:
    if Path(target).is_absolute() or target.startswith(("/", "\\")):
        return True
    root_resolved = root.resolve(strict=False)
    resolved_target = (link_path.parent / target).resolve(strict=False)
    try:
        return os.path.commonpath([str(root_resolved), str(resolved_target)]) != str(root_resolved)
    except ValueError:
        return True


def stat_key(st: os.stat_result) -> list[int]:
    # ctime cannot be set from userspace and changes on every write, chmod, or
    # utime, so it catches same-size rewrites that preserve mtime (cp -p, tar).
    if os.name == "nt":  # pragma: no cover - st_ctime is creation time on Windows
        return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode]
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode, st.st_ctime_ns]


def _entry_same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if a is None or b is None:
        return False
    return a.get("blob") == b.get("blob") and a.get("mode") == b.get("mode") and a.get("symlink_target") == b.get("symlink_target")


def _under(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _parent_dirs(paths: Iterable[str]) -> set[str]:
    dirs: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


# ---------------------------------------------------------------------------
# Checkout cache


class CheckoutCache:
    """Raw, uncompressed blob files used as clone sources.

    Files live at ``<root>/<hash[:2]>/<hash>`` (``.x`` suffix for executables),
    carry the fixed mtime, and are created atomically. The cache is disposable.
    """

    def __init__(self, root: Path, read_blob: Callable[[str], bytes]) -> None:
        self.root = Path(root)
        self.read_blob = read_blob

    def path_for(self, digest: str, blob_id: str, executable: bool) -> Path:
        path = self.root / digest[:2] / (digest + (".x" if executable else ""))
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(self.read_blob(blob_id))
        os.chmod(tmp, 0o755 if executable else 0o644)
        os.utime(tmp, ns=(_FIXED_NS, _FIXED_NS))
        os.replace(tmp, path)
        return path

    def size_bytes(self) -> int:
        total = 0
        if not self.root.exists():
            return 0
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
        return total


# ---------------------------------------------------------------------------
# Materialization records


@dataclass
class Attachment:
    """A directory placed at ``rel`` inside a materialization (e.g. an env output)."""

    rel: str
    source: Path
    mode: str  # "clone" | "link" | "copy" | "auto" (clone, else copy) | "clone-only" (clone, else skip)


@dataclass
class Materialization:
    path: Path
    backend: str
    degraded: bool
    baseline: dict[str, list[int]] = field(default_factory=dict)
    skip_prefixes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    used_base: bool = False
    clone_method: str | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def content_root(self) -> Path:
        """Directory to read changed files from after the command exits."""
        return self.path

    @property
    def writable_root(self) -> Path:
        """Directory Tri may drop files into before the command starts."""
        return self.path

    def wrap(self, argv: list[str]) -> list[str]:
        return list(argv)

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "degraded": self.degraded,
            "clone_method": self.clone_method,
            "used_base": self.used_base,
            "attachments": list(self.skip_prefixes),
            "skipped_entries": list(self.skipped),
            "notes": list(self.notes),
        }

    def save_baseline(self, file: Path) -> None:
        file = Path(file)
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "backend": self.backend,
            "degraded": self.degraded,
            "clone_method": self.clone_method,
            "skip_prefixes": self.skip_prefixes,
            "skipped": self.skipped,
            "baseline": self.baseline,
        }
        tmp = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, file)

    def update_baseline(self, observed: dict[str, list[int]], deleted: Iterable[str] = ()) -> None:
        """Record stats observed *before* changed files were read, so a write
        racing with an import still shows up on the next scan."""
        # A file modified within the last couple of seconds is "racily clean": on
        # coarse-timestamp filesystems a further same-size write could keep its
        # stat key, so leave the old key and let the next scan re-hash it.
        horizon = time.time_ns() - RACY_WINDOW_NS
        for path, key in observed.items():
            if len(key) > 1 and key[1] >= horizon:
                continue
            self.baseline[path] = key
        for path in deleted:
            self.baseline.pop(path, None)

    def refresh_baseline(self) -> None:
        self.baseline = take_baseline(self.path, self.skip_prefixes)

    def scan(self, entries: dict[str, dict[str, Any]], ignore: Any = None) -> tuple[list[str], list[str]]:
        """Return ``(changed_or_new, deleted)`` paths relative to ``entries``.

        A file is a candidate change when its stat key differs from the baseline
        or it is new; callers confirm by content hash. New files matched by
        ``ignore`` (an ``IgnoreRules``) are skipped, and ignored directories that
        hold no tracked files are not descended into.
        """
        return scan_directory(self.path, self.baseline, entries, ignore=ignore, skip_prefixes=self.skip_prefixes, skipped=self.skipped)

    def release(self) -> None:
        remove_tree(self.path)


def take_baseline(root: Path, skip_prefixes: Iterable[str] = ()) -> dict[str, list[int]]:
    skip = set(skip_prefixes)
    baseline: dict[str, list[int]] = {}

    def walk(directory: str, rel: str) -> None:
        try:
            it = os.scandir(directory)
        except OSError:
            return
        with it:
            for entry in it:
                child = f"{rel}/{entry.name}" if rel else entry.name
                if entry.name == ".git" or child in skip:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        walk(entry.path, child)
                    elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                        baseline[child] = stat_key(entry.stat(follow_symlinks=False))
                except OSError:
                    continue

    walk(str(root), "")
    return baseline


def scan_directory(
    root: Path,
    baseline: dict[str, list[int]],
    entries: dict[str, dict[str, Any]],
    *,
    ignore: Any = None,
    skip_prefixes: Iterable[str] = (),
    skipped: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    skip = set(skip_prefixes)
    skipped_set = set(skipped)
    tracked_dirs = _parent_dirs(entries)
    present: set[str] = set()
    changed: list[str] = []

    def walk(directory: str, rel: str) -> None:
        try:
            it = os.scandir(directory)
        except OSError:
            return
        with it:
            for entry in it:
                child = f"{rel}/{entry.name}" if rel else entry.name
                if entry.name == ".git" or child in skip:
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir:
                        if ignore is not None and child not in tracked_dirs and ignore.dir_ignored(child):
                            continue
                        walk(entry.path, child)
                        continue
                    if not (entry.is_file(follow_symlinks=False) or entry.is_symlink()):
                        continue
                    present.add(child)
                    key = baseline.get(child)
                    if key is not None and key == stat_key(entry.stat(follow_symlinks=False)):
                        continue
                    if child not in entries and ignore is not None and ignore.matches(child):
                        continue
                    changed.append(child)
                except OSError:
                    continue

    walk(str(root), "")
    deleted = [
        path
        for path in entries
        if path not in present and path not in skipped_set and not _under(path, skip)
    ]
    return sorted(changed), sorted(deleted)


def load_materialization(path: Path, baseline_file: Path) -> Materialization:
    payload = json.loads(Path(baseline_file).read_text())
    return Materialization(
        path=Path(path),
        backend=payload.get("backend", "copy"),
        degraded=bool(payload.get("degraded", True)),
        baseline={k: list(v) for k, v in payload.get("baseline", {}).items()},
        skip_prefixes=list(payload.get("skip_prefixes", [])),
        skipped=list(payload.get("skipped", [])),
        clone_method=payload.get("clone_method"),
    )


# ---------------------------------------------------------------------------
# Materializer


class Materializer:
    def __init__(
        self,
        home: Path,
        cache: CheckoutCache,
        *,
        read_blob: Callable[[str], bytes],
        load_tree: Callable[[str], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.home = Path(home)
        self.cache = cache
        self.read_blob = read_blob
        self.load_tree = load_tree
        self.bases = self.home / "bases"

    # -- capability -------------------------------------------------------

    def clone_method_for(self, directory: Path) -> str | None:
        """Clone method usable between the checkout cache and ``directory``."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.cache.root.mkdir(parents=True, exist_ok=True)
        try:
            if directory.stat().st_dev != self.cache.root.stat().st_dev:
                return None
        except OSError:
            return None
        return probe_clone(self.cache.root)

    def choose_backend(self, dest_parent: Path, requested: str | None = None) -> tuple[str, str | None, list[dict[str, Any]]]:
        requested = (requested or os.environ.get(BACKEND_ENV) or "auto").lower()
        notes: list[dict[str, Any]] = []
        if requested == "copy":
            return "copy", None, notes
        method = self.clone_method_for(dest_parent)
        if method is not None:
            return "clone", method, notes
        if requested == "clone":
            notes.append({"code": "clone_unavailable", "message": "Requested clone backend is not supported here; used copy."})
        return "copy", None, notes

    # -- placing files ----------------------------------------------------

    def _place(self, root: Path, path: str, entry: dict[str, Any], method: str | None, skipped: list[str]) -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = entry.get("mode", "100644")
        if mode == "120000":
            link = entry.get("symlink_target")
            if not isinstance(link, str):
                link = self.read_blob(entry["blob"]).decode(errors="replace")
            try:
                os.symlink(link, target)
            except OSError:
                # Windows without symlink privilege: leave the entry unmaterialized.
                skipped.append(path)
            return
        executable = mode == "100755"
        digest = entry.get("hash")
        if method is not None and isinstance(digest, str):
            src = self.cache.path_for(digest, entry["blob"], executable)
            try:
                clone_file(src, target, method)
            except FileNotFoundError:
                # Cache entry evicted between lookup and clone: recreate it once.
                src = self.cache.path_for(digest, entry["blob"], executable)
                clone_file(src, target, method)
            if method != "clonefile":
                os.chmod(target, 0o755 if executable else 0o644)
                os.utime(target, ns=(_FIXED_NS, _FIXED_NS))
            return
        target.write_bytes(self.read_blob(entry["blob"]))
        os.chmod(target, 0o755 if executable else 0o644)
        os.utime(target, ns=(_FIXED_NS, _FIXED_NS))

    def _check_symlinks(self, root: Path, entries: dict[str, dict[str, Any]]) -> None:
        for path, entry in entries.items():
            if entry.get("mode") != "120000":
                continue
            link = entry.get("symlink_target")
            if not isinstance(link, str):
                link = self.read_blob(entry["blob"]).decode(errors="replace")
            if symlink_target_escapes(root, root / path, link):
                raise MaterializeError("path_escaped_mount", "Stored symlink target escapes the materialized workspace root.", object=path, target=link)

    def ensure_base(self, tree_id: str, entries: dict[str, dict[str, Any]], method: str) -> Path | None:
        """Build (once) a pristine clone-source directory for a base tree."""
        final = self.bases / tree_id
        if final.is_dir():
            try:
                os.utime(final)
            except OSError:
                pass
            return final
        self.bases.mkdir(parents=True, exist_ok=True)
        tmp = self.bases / f".{tree_id}.{uuid.uuid4().hex}.tmp"
        try:
            skipped: list[str] = []
            if not self._derive_base(tmp, entries, method, skipped):
                tmp.mkdir()
                for path, entry in entries.items():
                    self._place(tmp, path, entry, method, skipped)
            os.rename(tmp, final)
        except OSError:
            remove_tree(tmp)
            return final if final.is_dir() else None
        return final

    def _derive_base(self, tmp: Path, entries: dict[str, dict[str, Any]], method: str, skipped: list[str]) -> bool:
        """Build a base by cloning the most recently used one and applying the
        difference, so a target that moved by one merge costs that merge's files
        instead of the whole tree. Returns False when no donor base is usable."""
        if self.load_tree is None:
            return False
        try:
            candidates = sorted(
                (child for child in self.bases.iterdir() if child.is_dir() and not child.name.startswith(".")),
                key=lambda child: child.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return False
        for donor in candidates[:3]:
            try:
                donor_entries = self.load_tree(donor.name)
            except Exception:
                continue
            if not clone_dir(donor, tmp):
                continue
            try:
                self._apply_delta(tmp, donor_entries, entries, method, skipped)
                return True
            except OSError:
                remove_tree(tmp)
        return False

    def _remove_path(self, root: Path, path: str) -> None:
        target = root / path
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            remove_tree(target)
        parent = target.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _apply_delta(self, root: Path, base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]], method: str | None, skipped: list[str]) -> None:
        for path, entry in base.items():
            if not _entry_same(entry, head.get(path)):
                self._remove_path(root, path)
        for path, entry in head.items():
            if _entry_same(entry, base.get(path)):
                continue
            target = root / path
            if target.is_dir() and not target.is_symlink():
                remove_tree(target)
            self._place(root, path, entry, method, skipped)

    def _attach(self, root: Path, attachment: Attachment, method: str | None, notes: list[dict[str, Any]]) -> bool:
        target = root / attachment.rel
        if target.exists() or target.is_symlink():
            notes.append({"code": "attachment_conflict", "path": attachment.rel, "message": "Snapshot already has files at this path; attachment skipped."})
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        source = Path(attachment.source)
        mode = attachment.mode
        if mode == "auto":
            # Tools write into dependency/build dirs (cargo target/, node_modules/.cache),
            # so a run needs a private, writable copy when the volume cannot clone.
            mode = "clone" if method is not None else "copy"
        elif mode == "clone-only":
            if method is None:
                notes.append({"code": "attachment_not_cloned", "path": attachment.rel, "message": "Volume cannot clone; shared directory not attached (use --env-link to symlink it)."})
                return False
            mode = "clone"
        if mode == "link":
            try:
                os.symlink(source, target, target_is_directory=True)
                return True
            except OSError as exc:
                # Windows without symlink privilege: fall back to a private copy.
                notes.append({"code": "attachment_link_failed", "path": attachment.rel, "message": f"Could not symlink the shared directory ({exc}); copied it instead."})
                mode = "copy"
        if mode == "clone" and method is not None:
            if clone_dir(source, target):
                _force_writable(target)
                return True

            def clone_copy(src: str, dst: str) -> None:
                try:
                    clone_file(Path(src), Path(dst), method)
                    shutil.copystat(src, dst)
                except OSError:
                    shutil.copy2(src, dst)

            shutil.copytree(source, target, symlinks=True, copy_function=clone_copy)
            _force_writable(target)
            return True
        shutil.copytree(source, target, symlinks=True)
        _force_writable(target)
        return True

    # -- entry point -------------------------------------------------------

    def materialize(
        self,
        entries: dict[str, dict[str, Any]],
        dest: Path,
        *,
        base: tuple[str, dict[str, dict[str, Any]]] | None = None,
        attachments: Iterable[Attachment] = (),
        backend: str | None = None,
    ) -> Materialization:
        dest = Path(dest)
        if dest.is_symlink():
            raise MaterializeError("path_escaped_mount", "Materialized workspace path cannot be a symlink.", object=str(dest))
        dest = dest.resolve()
        self._check_symlinks(dest, entries)
        if dest.exists():
            remove_tree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        chosen, method, notes = self.choose_backend(dest.parent, backend)
        skipped: list[str] = []
        used_base = False
        if method is not None and base is not None and clone_dir_supported():
            base_dir = self.ensure_base(base[0], base[1], method)
            if base_dir is not None and clone_dir(base_dir, dest):
                used_base = True
                self._apply_delta(dest, base[1], entries, method, skipped)
                skipped.extend(p for p in base[1] if base[1][p].get("mode") == "120000" and p in entries and not (dest / p).is_symlink() and p not in skipped)
        if not used_base:
            dest.mkdir(parents=True, exist_ok=True)
            for path, entry in entries.items():
                self._place(dest, path, entry, method, skipped)
        attached: list[str] = []
        for attachment in attachments:
            if self._attach(dest, attachment, method, notes):
                attached.append(attachment.rel)
        if skipped:
            notes.append({"code": "symlink_unsupported", "paths": sorted(set(skipped)), "message": "Symlinks could not be created here (Windows needs Developer Mode or elevation); these entries were not materialized."})
        mat = Materialization(
            path=dest,
            backend=chosen,
            degraded=chosen == "copy",
            skip_prefixes=attached,
            skipped=sorted(set(skipped)),
            used_base=used_base,
            clone_method=method,
            notes=notes,
        )
        mat.refresh_baseline()
        return mat
