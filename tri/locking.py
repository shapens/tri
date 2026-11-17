"""Cross-platform advisory file locks with shared and exclusive modes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _UnlockFileEx.restype = wintypes.BOOL
else:
    import fcntl


class FileLock:
    """An advisory lock on a file path.

    Shared locks may be held by many holders at once; an exclusive lock excludes
    every other holder. Locks are tied to this object's open file, so two
    ``FileLock`` objects in one process contend with each other like two processes.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: Any | None = self.path.open("a+b")
        self.held: str | None = None

    def fileno(self) -> int:
        if self._file is None:
            raise ValueError("lock is closed")
        return self._file.fileno()

    def acquire(self, *, exclusive: bool, blocking: bool = True) -> bool:
        if os.name == "nt":  # pragma: no cover
            ok = self._acquire_windows(exclusive=exclusive, blocking=blocking)
        else:
            flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(self.fileno(), flags)
                ok = True
            except (BlockingIOError, PermissionError):
                ok = False
        if ok:
            self.held = "exclusive" if exclusive else "shared"
        return ok

    def release(self) -> None:
        if self._file is None or self.held is None:
            return
        if os.name == "nt":  # pragma: no cover
            self._release_windows()
        else:
            fcntl.flock(self.fileno(), fcntl.LOCK_UN)
        self.held = None

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self.release()
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    if os.name == "nt":  # pragma: no cover

        def _handle(self) -> int:
            return msvcrt.get_osfhandle(self.fileno())

        def _acquire_windows(self, *, exclusive: bool, blocking: bool) -> bool:
            if self.held is not None:
                # LockFileEx does not convert lock modes in place.
                self._release_windows()
                self.held = None
            flags = _LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
            if not blocking:
                flags |= _LOCKFILE_FAIL_IMMEDIATELY
            overlapped = _OVERLAPPED()
            if _LockFileEx(self._handle(), flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
                return True
            error = ctypes.get_last_error()
            if error in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
                return False
            raise OSError(error, "LockFileEx failed", str(self.path))

        def _release_windows(self) -> None:
            overlapped = _OVERLAPPED()
            _UnlockFileEx(self._handle(), 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped))
