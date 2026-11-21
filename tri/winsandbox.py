"""Windows isolation for sandboxed runs: Job Objects plus a low-integrity token.

Without administrator rights a Windows process can still:

- put its children in a Job Object that kills every process in the job when
  the handle closes (or on timeout), caps committed memory, total CPU time,
  and the number of live processes, forbids breakaway, and blocks desktop,
  clipboard, and display changes;
- run them at low integrity. A low-integrity process cannot write any file or
  directory labeled medium or higher, which is every file without an explicit
  label. Tri labels only the run directory low, so that is the one place the
  command can write;
- label directories Tri owns (the Tri home, a runner's cache and token file)
  with a medium label that also forbids reads from lower integrity
  (`NO_READ_UP`), so the command cannot read the store.

What it cannot do without administrator rights, stated plainly:

- block the network. Windows Filtering Platform rules and firewall rules need
  administrator rights; AppContainer would block it but needs the command's
  toolchain to be readable by an AppContainer SID. `network_enforced` is false
  in every record this backend writes.
- hide files Tri does not own. Credential files under the user's profile
  (`.ssh`, `.aws`, ...) stay readable, because relabeling a user's files is
  not Tri's call. Low integrity still cannot write them.
- stop writes to the few locations that are labeled low by design
  (`%LOCALAPPDATA%Low`, `%TEMP%\\Low`).

The child is started through this file run as a script: it lowers its own
token to low integrity, then runs the command with inherited standard handles.
The parent creates it suspended, assigns it to the job, and only then lets it
run, so no process escapes the job.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

LOW_INTEGRITY_SID = "S-1-16-4096"
LOW_DIR_LABEL = "S:(ML;OICI;NW;;;LW)"
LOW_FILE_LABEL = "S:(ML;;NW;;;LW)"
HIDDEN_DIR_LABEL = "S:(ML;OICI;NRNW;;;ME)"
HIDDEN_FILE_LABEL = "S:(ML;;NRNW;;;ME)"
READABLE_DIR_LABEL = "S:(ML;OICI;NW;;;ME)"
DEFAULT_PROCESS_LIMIT = 64

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x00000010
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
SE_GROUP_INTEGRITY = 0x00000020
TOKEN_INTEGRITY_LEVEL = 25
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# Desktop, display settings, exit windows, global atoms, clipboard, system parameters.
JOB_OBJECT_UI_RESTRICTIONS = 0x02 | 0x04 | 0x08 | 0x10 | 0x20 | 0x40 | 0x80

_labeled: set[str] = set()


def helper_argv(argv: list[str]) -> list[str]:
    """The command line that runs argv at low integrity (used inside the job)."""
    return [sys.executable, "-I", str(Path(__file__).resolve()), "--", *argv]


# -- ctypes bindings ---------------------------------------------------------------


def _api() -> Any:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorSacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi32.GetSecurityDescriptorSacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    advapi32.SetTokenInformation.restype = wintypes.BOOL

    class API:
        pass

    api = API()
    api.ctypes = ctypes
    api.wintypes = wintypes
    api.kernel32 = kernel32
    api.advapi32 = advapi32
    api.ntdll = ntdll
    api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    api.JOBOBJECT_BASIC_UI_RESTRICTIONS = JOBOBJECT_BASIC_UI_RESTRICTIONS
    api.TOKEN_MANDATORY_LABEL = TOKEN_MANDATORY_LABEL
    return api


def _check(api: Any, ok: Any, what: str) -> None:
    if not ok:
        raise OSError(api.ctypes.get_last_error(), f"{what} failed: {api.ctypes.FormatError(api.ctypes.get_last_error())}")


# -- integrity labels ---------------------------------------------------------------


def current_label(path: Path) -> str:
    api = _api()
    ctypes, wintypes = api.ctypes, api.wintypes
    descriptor = ctypes.c_void_p()
    error = api.advapi32.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, None, ctypes.byref(descriptor))
    if error:
        raise OSError(error, f"GetNamedSecurityInfoW({path}) failed")
    text = wintypes.LPWSTR()
    try:
        _check(api, api.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, SDDL_REVISION_1, LABEL_SECURITY_INFORMATION, ctypes.byref(text), None), "ConvertSecurityDescriptorToStringSecurityDescriptorW")
        return text.value or ""
    finally:
        api.kernel32.LocalFree(descriptor)
        if text:
            api.kernel32.LocalFree(text)


def set_label(path: Path, sddl: str) -> None:
    """Set the mandatory integrity label on path. Inheritable labels propagate to existing children."""
    api = _api()
    ctypes, wintypes = api.ctypes, api.wintypes
    descriptor = ctypes.c_void_p()
    _check(api, api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None), "ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        sacl = ctypes.c_void_p()
        _check(api, api.advapi32.GetSecurityDescriptorSacl(descriptor, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)), "GetSecurityDescriptorSacl")
        error = api.advapi32.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, sacl)
        if error:
            raise OSError(error, f"SetNamedSecurityInfoW({path}) failed: {ctypes.FormatError(error)}")
    finally:
        api.kernel32.LocalFree(descriptor)


def parse_label(sddl: str) -> tuple[frozenset[str], frozenset[str], str] | None:
    """(ACE flags, access mask flags, integrity SID) of the mandatory label in an SDDL string."""
    start = sddl.find("(ML;")
    if start < 0:
        return None
    fields = sddl[start + 1 : sddl.index(")", start)].split(";")
    if len(fields) < 6:
        return None

    def pairs(text: str) -> frozenset[str]:
        return frozenset(text[index : index + 2] for index in range(0, len(text), 2))

    return pairs(fields[1]), pairs(fields[2]), fields[5]


def _label_once(path: Path, dir_label: str, file_label: str) -> None:
    """Label a Tri-owned path unless it already carries that explicit label (relabeling walks the tree)."""
    key = os.path.normcase(str(path))
    if key in _labeled or not path.exists():
        return
    wanted = dir_label if path.is_dir() else file_label
    if parse_label(current_label(path)) != parse_label(wanted):
        set_label(path, wanted)
    _labeled.add(key)


def prepare_labels(sandbox: Any) -> None:
    """Hide Tri-owned paths from low integrity, keep readable ones readable, and make the run directory writable."""
    for path in sandbox.relabel:
        _label_once(Path(path), HIDDEN_DIR_LABEL, HIDDEN_FILE_LABEL)
    for path in sandbox.readable:
        if any(_is_within(Path(path), Path(hidden)) for hidden in sandbox.relabel):
            _label_once(Path(path), READABLE_DIR_LABEL, READABLE_DIR_LABEL.replace("OICI", ""))
    for path in sandbox.writable:
        Path(path).mkdir(parents=True, exist_ok=True)
        set_label(Path(path), LOW_DIR_LABEL)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        Path(os.path.normcase(str(path))).relative_to(Path(os.path.normcase(str(parent))))
        return True
    except ValueError:
        return False


# -- job objects ----------------------------------------------------------------------


def create_job(limits: Any) -> Any:
    api = _api()
    ctypes = api.ctypes
    job = api.kernel32.CreateJobObjectW(None, None)
    _check(api, job, "CreateJobObjectW")
    info = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    info.BasicLimitInformation.ActiveProcessLimit = int(limits.pids or DEFAULT_PROCESS_LIMIT)
    if limits.cpu_seconds:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME
        info.BasicLimitInformation.PerJobUserTimeLimit = int(limits.cpu_seconds) * 10_000_000
    if limits.memory_mb:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = int(limits.memory_mb) * 1024 * 1024
    info.BasicLimitInformation.LimitFlags = flags  # no BREAKAWAY_OK: children cannot leave the job
    try:
        _check(api, api.kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)), "SetInformationJobObject(limits)")
        ui = api.JOBOBJECT_BASIC_UI_RESTRICTIONS(JOB_OBJECT_UI_RESTRICTIONS)
        _check(api, api.kernel32.SetInformationJobObject(job, JOB_OBJECT_BASIC_UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui)), "SetInformationJobObject(ui)")
    except OSError:
        api.kernel32.CloseHandle(job)
        raise
    return job


def run(sandbox: Any, argv: list[str], *, cwd: Path, env: dict[str, str]) -> Any:
    """Run argv at low integrity inside a fresh job; kill the whole job on timeout."""
    from tri.sandbox import Completed

    api = _api()
    try:
        prepare_labels(sandbox)
        job = create_job(sandbox.limits)
    except OSError as exc:
        return Completed(125, b"", f"[tri] cannot set up the Windows sandbox: {exc}\n".encode())
    timed_out = False
    try:
        proc = subprocess.Popen(
            helper_argv(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        )
        handle = int(proc._handle)  # type: ignore[attr-defined]
        if not api.kernel32.AssignProcessToJobObject(job, handle):
            error = api.ctypes.get_last_error()
            api.kernel32.TerminateProcess(handle, 125)
            proc.communicate()
            return Completed(125, b"", f"[tri] cannot place the command in a job object: {api.ctypes.FormatError(error)}\n".encode())
        status = api.ntdll.NtResumeProcess(handle)
        if status != 0:
            api.kernel32.TerminateJobObject(job, 125)
            proc.communicate()
            return Completed(125, b"", f"[tri] cannot resume the sandboxed command (NTSTATUS {status & 0xFFFFFFFF:#x})\n".encode())
        try:
            stdout, stderr = proc.communicate(timeout=sandbox.limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            api.kernel32.TerminateJobObject(job, 1)
            stdout, stderr = proc.communicate()
            stderr += f"\n[tri] killed after {sandbox.limits.timeout_seconds:g} s wall-clock limit\n".encode()
        return Completed(-9 if timed_out else proc.returncode, stdout, stderr, timed_out=timed_out)
    finally:
        api.kernel32.CloseHandle(job)  # KILL_ON_JOB_CLOSE: nothing outlives the run


# -- probe --------------------------------------------------------------------------------


PROBE_SCRIPT = r"""
import sys
seen = []
try:
    open(sys.argv[1], "rb").read(1)
    seen.append("read_hidden")
except OSError:
    pass
try:
    with open(sys.argv[2], "a") as handle:
        handle.write("x")
    seen.append("wrote_run")
except OSError as exc:
    seen.append("cannot_write_run:" + str(exc))
try:
    with open(sys.argv[3], "w") as handle:
        handle.write("x")
    seen.append("wrote_outside")
except OSError:
    pass
print(",".join(seen))
"""


def probe() -> dict[str, Any]:
    """Run a real low-integrity child in a job and check what it can reach."""
    import shutil
    import tempfile

    from tri.sandbox import Limits, Sandbox, scrubbed_env

    base: dict[str, Any] = {"kind": "none", "isolated": False, "backend": "winjob", "network_enforced": False}
    try:
        _api()
    except (OSError, AttributeError) as exc:
        return {**base, "reason": f"Windows security APIs are unavailable: {exc}"}
    root = Path(tempfile.mkdtemp(prefix="tri-winjob-probe-"))
    try:
        hidden = root / "hidden"
        (hidden / "nested").mkdir(parents=True)
        secret = hidden / "nested" / "secret.txt"
        secret.write_bytes(b"secret")
        run_dir = root / "run"
        (run_dir / "nested").mkdir(parents=True)
        existing = run_dir / "nested" / "existing.txt"
        existing.write_bytes(b"")
        outside = root / "outside.txt"
        box = Sandbox(kind="winjob", network=False, writable=[run_dir], hidden=[hidden], relabel=[hidden], limits=Limits(timeout_seconds=120))
        completed = run(box, [sys.executable, "-c", PROBE_SCRIPT, str(secret), str(existing), str(outside)], cwd=run_dir, env=scrubbed_env(run_dir))
        seen = completed.stdout.decode(errors="replace").strip()
        if completed.returncode != 0 or seen != "wrote_run" or outside.exists():
            detail = (seen or completed.stderr.decode(errors="replace").strip())[-300:]
            return {**base, "reason": f"the low-integrity job did not isolate as expected (exit {completed.returncode}): {detail}"}
        return {**base, "kind": "winjob", "isolated": True, "reason": None, "integrity": "low", "job_object": True}
    except OSError as exc:
        return {**base, "reason": f"cannot build the Windows sandbox: {exc}"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# -- helper (runs inside the job) ------------------------------------------------------------


def lower_own_integrity() -> None:
    api = _api()
    ctypes, wintypes = api.ctypes, api.wintypes
    token = wintypes.HANDLE()
    _check(api, api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), TOKEN_ADJUST_DEFAULT | TOKEN_QUERY, ctypes.byref(token)), "OpenProcessToken")
    sid = ctypes.c_void_p()
    try:
        _check(api, api.advapi32.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)), "ConvertStringSidToSidW")
        label = api.TOKEN_MANDATORY_LABEL()
        label.Label.Sid = sid
        label.Label.Attributes = SE_GROUP_INTEGRITY
        size = ctypes.sizeof(label) + api.advapi32.GetLengthSid(sid)
        _check(api, api.advapi32.SetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), size), "SetTokenInformation(TokenIntegrityLevel)")
    finally:
        if sid:
            api.kernel32.LocalFree(sid)
        api.kernel32.CloseHandle(token)


def _helper_main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        sys.stderr.write("[tri] no command for the Windows sandbox\n")
        return 2
    try:
        lower_own_integrity()
    except OSError as exc:
        sys.stderr.write(f"[tri] cannot drop to low integrity: {exc}\n")
        return 125
    sys.stdout.flush()
    try:
        return subprocess.call(argv, stdin=subprocess.DEVNULL)
    except OSError as exc:
        sys.stderr.write(f"[tri] cannot start {argv[0]}: {exc}\n")
        return 127


if __name__ == "__main__":
    sys.exit(_helper_main(sys.argv[1:]))
