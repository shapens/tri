"""Isolation for commands Tri runs on behalf of agents.

`tri run` executes code from a workspace the agent controls. On a shared
server that is remote code execution as the server user unless the command is
contained. A sandboxed run:

- sees the host filesystem read-only, except its own run directory
- cannot read the Tri home (the store holds hidden files and secret values)
  or well-known credential files under the user's home
- has no network unless the policy allows it
- starts from a scrubbed environment (no server credentials), with HOME and
  TMPDIR inside the run directory
- runs under CPU, memory, file-size, and wall-clock limits, and is killed as a
  process group when it exceeds them

Linux uses bubblewrap (`bwrap`, unprivileged user namespaces). macOS uses the
system sandbox (`sandbox-exec`). Elsewhere only limits and the scrubbed
environment apply, and `required` mode refuses to run.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODES = ("off", "auto", "required")
MODE_ENV = "TRI_RUN_SANDBOX"

SECRET_HOME_PATHS = (
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".config/gh",
    ".config/hub",
    ".config/gcloud",
    ".config/git/credentials",
)

ENV_PASSTHROUGH = ("PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "PYENV_VERSION", "PYENV_ROOT")


class SandboxUnavailable(Exception):
    pass


@dataclass
class Limits:
    timeout_seconds: float | None = None
    cpu_seconds: int | None = None
    memory_mb: int | None = None
    file_size_mb: int | None = None

    @classmethod
    def from_env(cls, *, timeout_seconds: float | None = None) -> "Limits":
        def number(name: str) -> float | None:
            value = os.environ.get(name)
            try:
                return float(value) if value else None
            except ValueError:
                return None

        cpu = number("TRI_RUN_CPU_SECONDS")
        memory = number("TRI_RUN_MEMORY_MB")
        file_size = number("TRI_RUN_FILE_SIZE_MB")
        return cls(
            timeout_seconds=timeout_seconds if timeout_seconds is not None else number("TRI_RUN_TIMEOUT"),
            cpu_seconds=int(cpu) if cpu else None,
            memory_mb=int(memory) if memory else None,
            file_size_mb=int(file_size) if file_size else None,
        )

    def describe(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "cpu_seconds": self.cpu_seconds, "memory_mb": self.memory_mb, "file_size_mb": self.file_size_mb}


@dataclass
class Sandbox:
    kind: str  # "bwrap", "seatbelt", or "none" (limits and environment only)
    network: bool
    writable: list[Path]
    readable: list[Path] = field(default_factory=list)
    hidden: list[Path] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)

    @property
    def isolated(self) -> bool:
        return self.kind in ("bwrap", "seatbelt")

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "isolated": self.isolated,
            "network": self.network,
            "writable": [str(path) for path in self.writable],
            "hidden_paths": len(self.hidden),
            "limits": self.limits.describe(),
        }

    def wrap(self, argv: list[str], cwd: Path) -> list[str]:
        if self.kind == "bwrap":
            return self._bwrap(argv, cwd)
        if self.kind == "seatbelt":
            return ["sandbox-exec", "-p", self._seatbelt_profile(), *argv]
        return list(argv)

    def _bwrap(self, argv: list[str], cwd: Path) -> list[str]:
        cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
        if not self.network:
            cmd.append("--unshare-net")
        cmd += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for path in self.hidden:
            if path.is_dir():
                cmd += ["--tmpfs", str(path)]
            elif path.exists():
                cmd += ["--ro-bind", "/dev/null", str(path)]
        for path in self.readable:
            if path.exists():
                cmd += ["--ro-bind", str(path), str(path)]
        for path in self.writable:
            cmd += ["--bind", str(path), str(path)]
        cmd += ["--chdir", str(cwd), "--"]
        return cmd + list(argv)

    def _seatbelt_profile(self) -> str:
        def quote(path: Path) -> str:
            return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

        rules = ["(version 1)", "(allow default)"]
        if not self.network:
            rules.append("(deny network*)")
            rules.append('(allow network* (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))')
        rules.append("(deny file-write*)")
        writable = " ".join(f"(subpath {quote(path)})" for path in self.writable)
        rules.append(f'(allow file-write* {writable} (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper") (regex #"^/dev/fd/") (regex #"^/dev/ttys"))')
        for path in self.hidden:
            rules.append(f"(deny file-read* (subpath {quote(path)}))")
        for path in self.readable + self.writable:
            rules.append(f"(allow file-read* (subpath {quote(path)}))")
        return "".join(rules)

    def preexec(self):  # type: ignore[no-untyped-def]
        limits = self.limits
        if os.name == "nt":
            return None

        def apply() -> None:
            import resource

            os.setsid()
            if limits.cpu_seconds:
                resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 5))
            if limits.memory_mb and sys.platform.startswith("linux"):
                size = limits.memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (size, size))
            if limits.file_size_mb:
                size = limits.file_size_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return apply


@dataclass
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def run(sandbox: Sandbox | None, argv: list[str], *, cwd: Path, env: dict[str, str]) -> Completed:
    """Run argv inside the sandbox (or directly when sandbox is None)."""
    if sandbox is None:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)
        return Completed(proc.returncode, proc.stdout, proc.stderr)
    kwargs: dict[str, Any] = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env}
    preexec = sandbox.preexec()
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    elif os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(sandbox.wrap(argv, cwd), **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=sandbox.limits.timeout_seconds)
        return Completed(proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        return Completed(-9, stdout, stderr + f"\n[tri] killed after {sandbox.limits.timeout_seconds:g} s wall-clock limit\n".encode(), timed_out=True)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


_PROBE: dict[str, Any] | None = None


def probe() -> dict[str, Any]:
    """Which isolation this host supports; cached per process."""
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    result: dict[str, Any] = {"kind": "none", "isolated": False, "reason": None}
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap") is None:
            result["reason"] = "bubblewrap (bwrap) is not installed"
        else:
            check = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-net", "--unshare-pid", "--proc", "/proc", "true"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if check.returncode == 0:
                result = {"kind": "bwrap", "isolated": True, "reason": None}
            else:
                result["reason"] = "bwrap cannot create namespaces here: " + check.stderr.decode(errors="replace").strip()[:200]
    elif sys.platform == "darwin":
        if shutil.which("sandbox-exec") is None:
            result["reason"] = "sandbox-exec is not available"
        else:
            check = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)", "true"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if check.returncode == 0:
                result = {"kind": "seatbelt", "isolated": True, "reason": None}
            else:
                result["reason"] = "sandbox-exec refused a trivial profile: " + check.stderr.decode(errors="replace").strip()[:200]
    else:
        result["reason"] = f"no sandbox backend for {sys.platform}"
    _PROBE = result
    return result


def resolve_mode(requested: str | None = None) -> str:
    mode = (requested or os.environ.get(MODE_ENV) or "off").lower()
    return mode if mode in MODES else "off"


def build(
    mode: str,
    *,
    run_dir: Path,
    hidden: list[Path],
    readable: list[Path] = (),  # type: ignore[assignment]
    network: bool = False,
    limits: Limits | None = None,
) -> Sandbox | None:
    """A sandbox for one command, or None when mode is off.

    Raises SandboxUnavailable in required mode on a host without isolation."""
    if mode == "off":
        return None
    info = probe()
    if not info["isolated"] and mode == "required":
        raise SandboxUnavailable(info.get("reason") or "no sandbox backend")
    home = Path.home()
    hidden_paths = [Path(os.path.realpath(path)) for path in hidden]
    hidden_paths += [Path(os.path.realpath(home / rel)) for rel in SECRET_HOME_PATHS if (home / rel).exists()]
    return Sandbox(
        kind=info["kind"],
        network=network,
        writable=[Path(os.path.realpath(run_dir))],
        readable=[Path(os.path.realpath(path)) for path in readable],
        hidden=hidden_paths,
        limits=limits or Limits(),
    )


def scrubbed_env(run_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a sandboxed command: no inherited credentials."""
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    home = Path(run_dir) / "home"
    tmp = Path(run_dir) / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(home), "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp), "TRI_SANDBOX": "1"})
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    env.update(extra or {})
    return env

