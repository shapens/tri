"""A pure-Python Linux FUSE server (no libfuse, no fusepy).

The kernel FUSE protocol is spoken directly over ``/dev/fuse``. Mounting is
unprivileged: ``fusermount3`` (setuid) opens ``/dev/fuse``, mounts it, and hands
the descriptor back over a socketpair named by ``_FUSE_COMMFD``. With
``auto_unmount`` fusermount3 stays behind and unmounts as soon as this process
dies for any reason, so a killed server does not leave a wedged mount. Without
it (very old fusermount), ``fusermount3 -u -z <dir>`` recovers one.

The server is single threaded and path based: inode numbers map to paths in
the view, and every operation is answered by a backend-neutral view object
(``tri.vfs.VirtualView``). The process serving the mount must never touch the
mount itself, or it would wait on its own reply.
"""

from __future__ import annotations

import errno
import os
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

# -- protocol constants (include/uapi/linux/fuse.h) ------------------------------

KERNEL_MAJOR = 7
KERNEL_MINOR = 31  # the newest protocol features this server relies on

LOOKUP, FORGET, GETATTR, SETATTR, READLINK, SYMLINK = 1, 2, 3, 4, 5, 6
MKNOD, MKDIR, UNLINK, RMDIR, RENAME, LINK = 8, 9, 10, 11, 12, 13
OPEN, READ, WRITE, STATFS, RELEASE, FSYNC = 14, 15, 16, 17, 18, 20
SETXATTR, GETXATTR, LISTXATTR, REMOVEXATTR, FLUSH, INIT = 21, 22, 23, 24, 25, 26
OPENDIR, READDIR, RELEASEDIR, FSYNCDIR = 27, 28, 29, 30
GETLK, SETLK, SETLKW, ACCESS, CREATE, INTERRUPT = 31, 32, 33, 34, 35, 36
BMAP, DESTROY, IOCTL, POLL, NOTIFY_REPLY, BATCH_FORGET = 37, 38, 39, 40, 41, 42
FALLOCATE, READDIRPLUS, RENAME2, LSEEK, COPY_FILE_RANGE = 43, 44, 45, 46, 47
SYNCFS, TMPFILE, STATX = 50, 51, 52

INIT_ASYNC_READ = 1 << 0
INIT_ATOMIC_O_TRUNC = 1 << 3
INIT_BIG_WRITES = 1 << 5
INIT_DO_READDIRPLUS = 1 << 13
INIT_READDIRPLUS_AUTO = 1 << 14
INIT_MAX_PAGES = 1 << 22
WANTED_INIT_FLAGS = INIT_ASYNC_READ | INIT_ATOMIC_O_TRUNC | INIT_BIG_WRITES | INIT_DO_READDIRPLUS | INIT_READDIRPLUS_AUTO | INIT_MAX_PAGES

FATTR_MODE, FATTR_UID, FATTR_GID, FATTR_SIZE = 1, 2, 4, 8
FATTR_ATIME, FATTR_MTIME, FATTR_FH = 16, 32, 64
FATTR_ATIME_NOW, FATTR_MTIME_NOW = 128, 256

FOPEN_KEEP_CACHE = 1 << 1
GETATTR_FH = 1
RENAME_NOREPLACE, RENAME_EXCHANGE = 1, 2

MAX_WRITE = 1 << 20
MAX_PAGES = MAX_WRITE // 4096
READ_BUFFER = MAX_WRITE + (1 << 16)
ROOT_ID = 1

IN_HEADER = struct.Struct("<IIQQIIIHH")  # len opcode unique nodeid uid gid pid total_extlen padding
OUT_HEADER = struct.Struct("<IiQ")  # len error unique
ATTR = struct.Struct("<QQQQQQIIIIIIIIII")  # ino size blocks atime mtime ctime (+nsec x3) mode nlink uid gid rdev blksize flags
ENTRY_HEAD = struct.Struct("<QQQQII")  # nodeid generation entry_valid attr_valid entry_valid_nsec attr_valid_nsec
ATTR_OUT_HEAD = struct.Struct("<QII")  # attr_valid attr_valid_nsec dummy
INIT_IN = struct.Struct("<IIII")
INIT_OUT = struct.Struct("<IIIIHHIIHHII24x")
OPEN_OUT = struct.Struct("<QII")
READ_IN = struct.Struct("<QQIIQII")  # fh offset size read_flags lock_owner flags padding
WRITE_IN = struct.Struct("<QQIIQII")
WRITE_OUT = struct.Struct("<II")
RELEASE_IN = struct.Struct("<QIIQ")
GETATTR_IN = struct.Struct("<IIQ")
SETATTR_IN = struct.Struct("<IIQQQQQQIIIIIIII")
CREATE_IN = struct.Struct("<IIII")
MKDIR_IN = struct.Struct("<II")
MKNOD_IN = struct.Struct("<IIII")
RENAME_IN = struct.Struct("<Q")
RENAME2_IN = struct.Struct("<QII")
OPEN_IN = struct.Struct("<II")
FORGET_IN = struct.Struct("<Q")
BATCH_FORGET_IN = struct.Struct("<II")
FORGET_ONE = struct.Struct("<QQ")
FSYNC_IN = struct.Struct("<QII")
DIRENT_HEAD = struct.Struct("<QQII")  # ino off namelen type
STATFS_OUT = struct.Struct("<QQQQQIIII24x")

ENTRY_TTL = 10.0
ATTR_TTL = 10.0
UPPER_ATTR_TTL = 1.0
NO_REPLY = object()


class Attr:
    """What a view reports for one path (inode number assigned by the server)."""

    __slots__ = ("mode", "size", "mtime_ns", "nlink", "upper")

    def __init__(self, mode: int, size: int, mtime_ns: int, nlink: int = 1, upper: bool = False) -> None:
        self.mode = mode
        self.size = size
        self.mtime_ns = mtime_ns
        self.nlink = nlink
        self.upper = upper


# -- availability -----------------------------------------------------------------


def fusermount_binary() -> str | None:
    return shutil.which("fusermount3") or shutil.which("fusermount")


def platform_supported() -> bool:
    return sys.platform.startswith("linux")


def probe(*, deep: bool = False) -> dict[str, Any]:
    """Can this host serve a FUSE view unprivileged? ``deep`` performs a real
    mount and unmount in a temporary directory."""
    info: dict[str, Any] = {"backend": "fuse", "available": False, "dev_fuse": os.path.exists("/dev/fuse"), "fusermount": fusermount_binary()}
    if not platform_supported():
        info["reason"] = "not_linux"
        return info
    if not info["dev_fuse"]:
        info["reason"] = "no_dev_fuse"
        return info
    if not os.access("/dev/fuse", os.R_OK | os.W_OK):
        info["reason"] = "dev_fuse_not_accessible"
        return info
    if info["fusermount"] is None:
        info["reason"] = "no_fusermount3"
        return info
    if deep:
        import tempfile

        target = Path(tempfile.mkdtemp(prefix="tri-fuse-probe-"))
        try:
            mount = FuseMount(target, auto_unmount=False)
            try:
                mount.mount()
            except OSError as exc:
                info["reason"] = "mount_failed"
                info["error"] = str(exc)
                return info
            mount.unmount()
        finally:
            try:
                target.rmdir()
            except OSError:
                pass
        info["probed"] = True
    info["available"] = True
    info["reason"] = None
    return info


# -- mounting ---------------------------------------------------------------------


class FuseMount:
    """One unprivileged FUSE mount made through fusermount3."""

    def __init__(self, mountpoint: Path, *, read_only: bool = False, fsname: str = "tri", auto_unmount: bool = True) -> None:
        self.mountpoint = Path(mountpoint)
        self.read_only = read_only
        self.fsname = fsname
        self.auto_unmount = auto_unmount
        self.fd: int | None = None
        self._sock: socket.socket | None = None
        self._helper: subprocess.Popen | None = None

    def _options(self, auto_unmount: bool) -> str:
        opts = ["ro" if self.read_only else "rw", "nosuid", "nodev", "default_permissions", f"fsname={self.fsname}", "subtype=tri"]
        if auto_unmount:
            opts.append("auto_unmount")
        return ",".join(opts)

    def mount(self) -> int:
        binary = fusermount_binary()
        if binary is None:
            raise OSError(errno.ENOENT, "fusermount3 is not installed")
        errors = []
        for auto in ([True, False] if self.auto_unmount else [False]):
            try:
                self.fd = self._mount_once(binary, auto)
                self.auto_unmount = auto
                return self.fd
            except OSError as exc:
                errors.append(str(exc))
        raise OSError(errno.EIO, "; ".join(errors))

    def _mount_once(self, binary: str, auto_unmount: bool) -> int:
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        env = dict(os.environ, _FUSE_COMMFD=str(theirs.fileno()))
        try:
            helper = subprocess.Popen(
                [binary, "-o", self._options(auto_unmount), "--", str(self.mountpoint)],
                env=env,
                pass_fds=(theirs.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        finally:
            theirs.close()
        try:
            _, fds, _, _ = socket.recv_fds(ours, 1, 1)
        except OSError:
            fds = []
        if not fds:
            ours.close()
            try:
                _, err = helper.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                helper.kill()
                err = b"fusermount3 did not answer"
            raise OSError(errno.EIO, (err or b"fusermount3 returned no descriptor").decode(errors="replace").strip())
        if auto_unmount:
            # fusermount3 waits on this socket and unmounts when it closes
            # (this process exiting, however it exits). Keep it open.
            self._sock = ours
            self._helper = helper
            if helper.stderr is not None:
                helper.stderr.close()
        else:
            ours.close()
            helper.communicate(timeout=10)
        os.set_inheritable(fds[0], False)
        return fds[0]

    def unmount(self, *, lazy: bool = True) -> bool:
        ok = unmount(self.mountpoint, lazy=lazy)
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._helper is not None:
            try:
                self._helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._helper = None
        return ok


def is_mounted(mountpoint: Path) -> bool:
    target = os.path.abspath(str(mountpoint))  # callers pass resolved paths; never stat a possibly dead mount
    try:
        with open("/proc/self/mounts", "rb") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3 and parts[2].startswith(b"fuse") and _unescape_mount(parts[1]) == target:
                    return True
    except OSError:
        return False
    return False


def _unescape_mount(raw: bytes) -> str:
    text = raw.decode(errors="surrogateescape")
    for code in ("040", "011", "012", "134"):
        text = text.replace("\\" + code, chr(int(code, 8)))
    return text


def unmount(mountpoint: Path, *, lazy: bool = True) -> bool:
    """Unmount a FUSE view, also one whose server died (``ENOTCONN``)."""
    binary = fusermount_binary()
    if binary is None:
        return False
    argv = [binary, "-u", *(["-z"] if lazy else []), "-q", "--", str(mountpoint)]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


# -- serving ----------------------------------------------------------------------


def _name(body: memoryview) -> str:
    raw = bytes(body)
    end = raw.find(b"\0")
    return os.fsdecode(raw[: end if end >= 0 else len(raw)])


def _two_names(body: memoryview) -> tuple[str, str]:
    raw = bytes(body)
    first, _, rest = raw.partition(b"\0")
    second = rest.split(b"\0", 1)[0]
    return os.fsdecode(first), os.fsdecode(second)


def _split_ttl(seconds: float) -> tuple[int, int]:
    whole = int(seconds)
    return whole, int((seconds - whole) * 1e9)


class FuseServer:
    """Serve one view over a /dev/fuse descriptor until unmounted or stopped."""

    def __init__(self, fd: int, view: Any, *, on_idle: Callable[[], None] | None = None) -> None:
        self.fd = fd
        self.view = view
        self.on_idle = on_idle
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.path_of: dict[int, str | None] = {ROOT_ID: ""}
        self.ino_of: dict[str, int] = {"": ROOT_ID}
        self.lookups: dict[int, int] = {}
        self.next_ino = ROOT_ID + 1
        self.handles: dict[int, Any] = {}
        self.dir_handles: dict[int, list[tuple[str, str | None]]] = {}
        self.next_fh = 1
        self.readdirplus = False
        self.stop_requested = False
        self.stop_deadline: float | None = None
        self.initialized = False
        self.handlers: dict[int, Callable[[int, memoryview], Any]] = {
            INIT: self.op_init,
            LOOKUP: self.op_lookup,
            FORGET: self.op_forget,
            BATCH_FORGET: self.op_batch_forget,
            GETATTR: self.op_getattr,
            SETATTR: self.op_setattr,
            READLINK: self.op_readlink,
            SYMLINK: self.op_symlink,
            MKNOD: self.op_mknod,
            MKDIR: self.op_mkdir,
            UNLINK: self.op_unlink,
            RMDIR: self.op_rmdir,
            RENAME: self.op_rename,
            RENAME2: self.op_rename2,
            OPEN: self.op_open,
            READ: self.op_read,
            WRITE: self.op_write,
            STATFS: self.op_statfs,
            RELEASE: self.op_release,
            FSYNC: self.op_fsync,
            FLUSH: self.op_ok,
            OPENDIR: self.op_opendir,
            READDIR: self.op_readdir,
            READDIRPLUS: self.op_readdirplus,
            RELEASEDIR: self.op_releasedir,
            FSYNCDIR: self.op_ok,
            CREATE: self.op_create,
            INTERRUPT: self.op_no_reply,
            NOTIFY_REPLY: self.op_no_reply,
            DESTROY: self.op_destroy,
            SYNCFS: self.op_ok,
        }

    # -- loop -----------------------------------------------------------------

    def request_stop(self, grace: float = 5.0) -> None:
        self.stop_requested = True
        self.stop_deadline = time.monotonic() + grace

    def serve(self) -> None:
        while True:
            if self.stop_deadline is not None and time.monotonic() > self.stop_deadline:
                return
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.25)
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    return
                raise
            if not ready:
                if self.on_idle is not None:
                    self.on_idle()
                continue
            try:
                buf = os.read(self.fd, READ_BUFFER)
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.ENOENT):
                    continue
                if exc.errno in (errno.ENODEV, errno.EBADF):
                    return  # unmounted
                raise
            if not buf:
                return
            self.dispatch(buf)

    def dispatch(self, buf: bytes) -> None:
        length, opcode, unique, nodeid, _uid, _gid, _pid, _extlen, _pad = IN_HEADER.unpack_from(buf)
        body = memoryview(buf)[IN_HEADER.size : length]
        handler = self.handlers.get(opcode)
        if handler is None:
            self.reply_error(unique, errno.ENOSYS)
            return
        try:
            result = handler(nodeid, body)
        except OSError as exc:
            self.reply_error(unique, exc.errno or errno.EIO)
            return
        except Exception as exc:  # a bug answers EIO instead of wedging the mount
            sys.stderr.write(f"[tri fuse] opcode {opcode}: {type(exc).__name__}: {exc}\n")
            self.reply_error(unique, errno.EIO)
            return
        if result is NO_REPLY:
            return
        self.reply(unique, result or b"")

    def reply(self, unique: int, payload: bytes) -> None:
        try:
            os.write(self.fd, OUT_HEADER.pack(OUT_HEADER.size + len(payload), 0, unique) + payload)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENODEV, errno.EBADF):  # interrupted request / unmounted
                raise

    def reply_error(self, unique: int, err: int) -> None:
        try:
            os.write(self.fd, OUT_HEADER.pack(OUT_HEADER.size, -err, unique))
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENODEV, errno.EBADF):
                raise

    # -- inode bookkeeping ----------------------------------------------------

    def path(self, nodeid: int) -> str:
        path = self.path_of.get(nodeid)
        if path is None:
            raise OSError(errno.ENOENT, "stale inode")
        return path

    def child(self, nodeid: int, name: str) -> str:
        if not name or name in (".", "..") or "/" in name:
            raise OSError(errno.EINVAL, "bad name")
        parent = self.path(nodeid)
        return f"{parent}/{name}" if parent else name

    def ino(self, path: str) -> int:
        ino = self.ino_of.get(path)
        if ino is None:
            ino = self.next_ino
            self.next_ino += 1
            self.ino_of[path] = ino
            self.path_of[ino] = path
        return ino

    def drop_path(self, path: str) -> None:
        ino = self.ino_of.pop(path, None)
        if ino is not None and ino != ROOT_ID:
            self.path_of[ino] = None
            if not self.lookups.get(ino):
                self.path_of.pop(ino, None)

    def move_paths(self, old: str, new: str) -> None:
        prefix = old + "/"
        moved = [(path, ino) for path, ino in self.ino_of.items() if path == old or path.startswith(prefix)]
        for path, _ in moved:
            del self.ino_of[path]
        for path, ino in moved:
            target = new + path[len(old) :]
            self.ino_of[target] = ino
            self.path_of[ino] = target

    def attr_bytes(self, ino: int, attr: Attr) -> bytes:
        sec, nsec = divmod(attr.mtime_ns, 1_000_000_000)
        blocks = (attr.size + 511) // 512
        return ATTR.pack(ino, attr.size, blocks, sec, sec, sec, nsec, nsec, nsec, attr.mode, attr.nlink, self.uid, self.gid, 0, 4096, 0)

    def entry_bytes(self, path: str, attr: Attr) -> bytes:
        ino = self.ino(path)
        self.lookups[ino] = self.lookups.get(ino, 0) + 1
        entry_s, entry_ns = _split_ttl(ENTRY_TTL)
        attr_s, attr_ns = _split_ttl(UPPER_ATTR_TTL if attr.upper else ATTR_TTL)
        return ENTRY_HEAD.pack(ino, 0, entry_s, attr_s, entry_ns, attr_ns) + self.attr_bytes(ino, attr)

    def attr_out(self, ino: int, attr: Attr) -> bytes:
        attr_s, attr_ns = _split_ttl(UPPER_ATTR_TTL if attr.upper else ATTR_TTL)
        return ATTR_OUT_HEAD.pack(attr_s, attr_ns, 0) + self.attr_bytes(ino, attr)

    def new_fh(self, handle: Any) -> int:
        fh = self.next_fh
        self.next_fh += 1
        self.handles[fh] = handle
        return fh

    # -- operations -----------------------------------------------------------

    def op_ok(self, nodeid: int, body: memoryview) -> bytes:
        return b""

    def op_no_reply(self, nodeid: int, body: memoryview) -> Any:
        return NO_REPLY

    def op_init(self, nodeid: int, body: memoryview) -> bytes:
        major, minor, max_readahead, flags = INIT_IN.unpack_from(body)
        if major < KERNEL_MAJOR:
            raise OSError(errno.EPROTO, "kernel FUSE protocol too old")
        if major > KERNEL_MAJOR:
            # Ask the kernel to retry at our major version.
            return INIT_OUT.pack(KERNEL_MAJOR, KERNEL_MINOR, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        out_minor = min(minor, KERNEL_MINOR)
        out_flags = flags & WANTED_INIT_FLAGS
        self.readdirplus = bool(out_flags & INIT_DO_READDIRPLUS)
        self.initialized = True
        payload = INIT_OUT.pack(KERNEL_MAJOR, out_minor, max_readahead, out_flags, 16, 12, MAX_WRITE, 1, MAX_PAGES, 0, 0, 0)
        return payload if minor >= 23 else payload[:24]

    def op_destroy(self, nodeid: int, body: memoryview) -> bytes:
        self.request_stop(0.5)
        return b""

    def op_lookup(self, nodeid: int, body: memoryview) -> bytes:
        path = self.child(nodeid, _name(body))
        attr = self.view.getattr(path)
        return self.entry_bytes(path, attr)

    def _forget(self, nodeid: int, count: int) -> None:
        if nodeid == ROOT_ID:
            return
        left = self.lookups.get(nodeid, 0) - count
        if left > 0:
            self.lookups[nodeid] = left
            return
        self.lookups.pop(nodeid, None)
        path = self.path_of.get(nodeid)
        if path is None:
            self.path_of.pop(nodeid, None)

    def op_forget(self, nodeid: int, body: memoryview) -> Any:
        (count,) = FORGET_IN.unpack_from(body)
        self._forget(nodeid, count)
        return NO_REPLY

    def op_batch_forget(self, nodeid: int, body: memoryview) -> Any:
        count, _ = BATCH_FORGET_IN.unpack_from(body)
        offset = BATCH_FORGET_IN.size
        for _ in range(count):
            one, many = FORGET_ONE.unpack_from(body, offset)
            offset += FORGET_ONE.size
            self._forget(one, many)
        return NO_REPLY

    def op_getattr(self, nodeid: int, body: memoryview) -> bytes:
        flags, _, fh = GETATTR_IN.unpack_from(body) if len(body) >= GETATTR_IN.size else (0, 0, 0)
        path = self.path_of.get(nodeid)
        if path is None:
            handle = self.handles.get(fh) if flags & GETATTR_FH else None
            if handle is None or not hasattr(handle, "fstat"):
                raise OSError(errno.ENOENT, "stale inode")
            return self.attr_out(nodeid, handle.fstat())
        return self.attr_out(nodeid, self.view.getattr(path))

    def op_setattr(self, nodeid: int, body: memoryview) -> bytes:
        fields = SETATTR_IN.unpack_from(body)
        valid, _, fh, size, _lock, atime, mtime, _ctime, atimensec, mtimensec, _cns, mode, _u4, uid, gid, _u5 = fields
        path = self.path(nodeid)
        if valid & (FATTR_UID | FATTR_GID):
            if (valid & FATTR_UID and uid != self.uid) or (valid & FATTR_GID and gid != self.gid):
                raise OSError(errno.EPERM, "ownership is fixed")
        if valid & FATTR_SIZE:
            handle = self.handles.get(fh) if valid & FATTR_FH else None
            if handle is not None and hasattr(handle, "truncate"):
                handle.truncate(size)
            else:
                self.view.truncate(path, size)
        if valid & FATTR_MODE:
            self.view.chmod(path, mode & 0o7777)
        if valid & (FATTR_ATIME | FATTR_MTIME | FATTR_ATIME_NOW | FATTR_MTIME_NOW):
            now = time.time_ns()
            a = now if valid & FATTR_ATIME_NOW else (atime * 1_000_000_000 + atimensec if valid & FATTR_ATIME else None)
            m = now if valid & FATTR_MTIME_NOW else (mtime * 1_000_000_000 + mtimensec if valid & FATTR_MTIME else None)
            self.view.utimens(path, a, m)
        return self.attr_out(nodeid, self.view.getattr(path))

    def op_readlink(self, nodeid: int, body: memoryview) -> bytes:
        return os.fsencode(self.view.readlink(self.path(nodeid)))

    def op_symlink(self, nodeid: int, body: memoryview) -> bytes:
        name, target = _two_names(body)
        path = self.child(nodeid, name)
        self.view.symlink(path, target)
        return self.entry_bytes(path, self.view.getattr(path))

    def op_mknod(self, nodeid: int, body: memoryview) -> bytes:
        mode, _rdev, umask, _ = MKNOD_IN.unpack_from(body)
        if not stat.S_ISREG(mode):
            raise OSError(errno.EPERM, "only regular files")
        path = self.child(nodeid, _name(body[MKNOD_IN.size :]))
        handle = self.view.create(path, mode & ~umask & 0o7777, os.O_WRONLY | os.O_EXCL)
        handle.release()
        return self.entry_bytes(path, self.view.getattr(path))

    def op_mkdir(self, nodeid: int, body: memoryview) -> bytes:
        mode, umask = MKDIR_IN.unpack_from(body)
        path = self.child(nodeid, _name(body[MKDIR_IN.size :]))
        self.view.mkdir(path, mode & ~umask & 0o7777)
        return self.entry_bytes(path, self.view.getattr(path))

    def op_unlink(self, nodeid: int, body: memoryview) -> bytes:
        path = self.child(nodeid, _name(body))
        self.view.unlink(path)
        self.drop_path(path)
        return b""

    def op_rmdir(self, nodeid: int, body: memoryview) -> bytes:
        path = self.child(nodeid, _name(body))
        self.view.rmdir(path)
        self.drop_path(path)
        return b""

    def _rename(self, nodeid: int, newdir: int, names: memoryview, flags: int) -> bytes:
        if flags & RENAME_EXCHANGE:
            raise OSError(errno.EINVAL, "RENAME_EXCHANGE is not supported")
        old_name, new_name = _two_names(names)
        old = self.child(nodeid, old_name)
        new = self.child(newdir, new_name)
        if old == new:
            return b""
        self.view.rename(old, new, noreplace=bool(flags & RENAME_NOREPLACE))
        self.drop_path(new)
        self.move_paths(old, new)
        return b""

    def op_rename(self, nodeid: int, body: memoryview) -> bytes:
        (newdir,) = RENAME_IN.unpack_from(body)
        return self._rename(nodeid, newdir, body[RENAME_IN.size :], 0)

    def op_rename2(self, nodeid: int, body: memoryview) -> bytes:
        newdir, flags, _ = RENAME2_IN.unpack_from(body)
        return self._rename(nodeid, newdir, body[RENAME2_IN.size :], flags)

    def op_open(self, nodeid: int, body: memoryview) -> bytes:
        flags, _ = OPEN_IN.unpack_from(body)
        handle = self.view.open(self.path(nodeid), flags)
        open_flags = FOPEN_KEEP_CACHE if getattr(handle, "keep_cache", False) else 0
        return OPEN_OUT.pack(self.new_fh(handle), open_flags, 0)

    def op_create(self, nodeid: int, body: memoryview) -> bytes:
        flags, mode, umask, _ = CREATE_IN.unpack_from(body)
        path = self.child(nodeid, _name(body[CREATE_IN.size :]))
        handle = self.view.create(path, mode & ~umask & 0o7777, flags)
        entry = self.entry_bytes(path, self.view.getattr(path))
        return entry + OPEN_OUT.pack(self.new_fh(handle), 0, 0)

    def op_read(self, nodeid: int, body: memoryview) -> bytes:
        fh, offset, size, *_ = READ_IN.unpack_from(body)
        handle = self.handles.get(fh)
        if handle is None:
            raise OSError(errno.EBADF, "unknown handle")
        return handle.read(offset, size)

    def op_write(self, nodeid: int, body: memoryview) -> bytes:
        fh, offset, size, *_ = WRITE_IN.unpack_from(body)
        handle = self.handles.get(fh)
        if handle is None:
            raise OSError(errno.EBADF, "unknown handle")
        data = body[WRITE_IN.size : WRITE_IN.size + size]
        written = handle.write(offset, data)
        return WRITE_OUT.pack(written, 0)

    def op_release(self, nodeid: int, body: memoryview) -> bytes:
        fh = RELEASE_IN.unpack_from(body)[0]
        handle = self.handles.pop(fh, None)
        if handle is not None:
            handle.release()
        return b""

    def op_fsync(self, nodeid: int, body: memoryview) -> bytes:
        fh = FSYNC_IN.unpack_from(body)[0]
        handle = self.handles.get(fh)
        if handle is not None and hasattr(handle, "fsync"):
            handle.fsync()
        return b""

    def op_statfs(self, nodeid: int, body: memoryview) -> bytes:
        files = self.view.file_count() if hasattr(self.view, "file_count") else 0
        free = 1 << 30
        return STATFS_OUT.pack(free * 2, free, free, files + free, free, 4096, 255, 4096, 0)

    def op_opendir(self, nodeid: int, body: memoryview) -> bytes:
        path = self.path(nodeid)
        listing = self.view.listdir(path)
        entries: list[tuple[str, str | None]] = [(".", None), ("..", None)]
        entries.extend((name, f"{path}/{name}" if path else name) for name in listing)
        fh = self.next_fh
        self.next_fh += 1
        self.dir_handles[fh] = entries
        return OPEN_OUT.pack(fh, 0, 0)

    def _dirent(self, ino: int, offset: int, name: str, mode: int) -> bytes:
        raw = os.fsencode(name)
        dtype = (mode & 0o170000) >> 12
        record = DIRENT_HEAD.pack(ino, offset, len(raw), dtype) + raw
        return record + b"\0" * ((8 - len(record) % 8) % 8)

    def op_readdir(self, nodeid: int, body: memoryview) -> bytes:
        fh, offset, size, *_ = READ_IN.unpack_from(body)
        entries = self.dir_handles.get(fh)
        if entries is None:
            raise OSError(errno.EBADF, "unknown directory handle")
        out = bytearray()
        for index in range(offset, len(entries)):
            name, path = entries[index]
            if path is None:
                record = self._dirent(nodeid if name == "." else 0xFFFFFFFF, index + 1, name, stat.S_IFDIR)
            else:
                try:
                    attr = self.view.getattr(path)
                except OSError:
                    continue
                record = self._dirent(self.ino_of.get(path, 0xFFFFFFFF), index + 1, name, attr.mode)
            if len(out) + len(record) > size:
                break
            out += record
        return bytes(out)

    def op_readdirplus(self, nodeid: int, body: memoryview) -> bytes:
        fh, offset, size, *_ = READ_IN.unpack_from(body)
        entries = self.dir_handles.get(fh)
        if entries is None:
            raise OSError(errno.EBADF, "unknown directory handle")
        out = bytearray()
        empty_entry = ENTRY_HEAD.pack(0, 0, 0, 0, 0, 0) + ATTR.pack(*([0] * 16))
        for index in range(offset, len(entries)):
            name, path = entries[index]
            if path is None:
                record = empty_entry + self._dirent(0xFFFFFFFF, index + 1, name, stat.S_IFDIR)
                if len(out) + len(record) > size:
                    break
                out += record
                continue
            try:
                attr = self.view.getattr(path)
            except OSError:
                continue
            ino = self.ino(path)
            dirent = self._dirent(ino, index + 1, name, attr.mode)
            if len(out) + ENTRY_HEAD.size + ATTR.size + len(dirent) > size:
                break
            out += self.entry_bytes(path, attr) + dirent  # counts one lookup, as the kernel expects
        return bytes(out)

    def op_releasedir(self, nodeid: int, body: memoryview) -> bytes:
        fh = RELEASE_IN.unpack_from(body)[0]
        self.dir_handles.pop(fh, None)
        return b""


def install_stop_handlers(server: FuseServer, mount: FuseMount) -> None:
    """SIGTERM/SIGINT/SIGHUP: unmount in the background (the unmount must not
    wait on this process) and keep serving until the kernel says it is gone."""
    started: list[subprocess.Popen] = []

    def handler(signum: int, frame: Any) -> None:
        if server.stop_requested:
            return
        server.request_stop(10.0)
        binary = fusermount_binary()
        if binary is not None:
            try:
                started.append(subprocess.Popen([binary, "-u", "-z", "-q", "--", str(mount.mountpoint)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            except OSError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handler)
