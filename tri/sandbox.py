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

Backends (`TRI_RUN_SANDBOX_BACKEND`, default `auto`):

- `bwrap`: Linux bubblewrap (unprivileged user namespaces). `auto` on Linux.
- `seatbelt`: the macOS system sandbox (`sandbox-exec`). `auto` on macOS.
- `winjob`: Windows Job Objects plus a low-integrity token (`tri/winsandbox.py`).
  `auto` on Windows. It cannot block the network without admin rights.
- `container`: an OCI container through the docker or podman CLI
  (`TRI_RUN_OCI_ENGINE`), with `--network none` unless allowed, a read-only
  root, only the run directory bound writable, a non-root user, all
  capabilities dropped, and CPU, memory, pids, and wall-clock limits. The
  container is killed and removed on timeout. `TRI_RUN_OCI_RUNTIME` picks the
  OCI runtime: `runc` (default), `runsc` (gVisor, a user-space kernel), or
  `kata-fc` (Kata Containers on Firecracker microVMs). `TRI_RUN_OCI_IMAGE`
  picks the image the command runs in.

Elsewhere only limits and the scrubbed environment apply, and `required` mode
refuses to run.
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
BACKENDS = ("auto", "bwrap", "seatbelt", "winjob", "container")
BACKEND_ENV = "TRI_RUN_SANDBOX_BACKEND"
ISOLATED_KINDS = ("bwrap", "seatbelt", "winjob", "container")
OCI_ENGINE_ENV = "TRI_RUN_OCI_ENGINE"
OCI_RUNTIME_ENV = "TRI_RUN_OCI_RUNTIME"
OCI_IMAGE_ENV = "TRI_RUN_OCI_IMAGE"
OCI_USER_ENV = "TRI_RUN_OCI_USER"
OCI_DEFAULT_IMAGE = "docker.io/library/python:3.11-slim"
OCI_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
OCI_DEFAULT_PIDS = 512
# Variables the engine CLI itself needs; the container never sees the host's.
OCI_INLINE_ENV = ("HOME", "TMPDIR", "TMP", "TEMP", "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TRI_SANDBOX", "USERPROFILE")

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
    pids: int | None = None
    cpus: float | None = None

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
        pids = number("TRI_RUN_PIDS")
        cpus = number("TRI_RUN_CPUS")
        return cls(
            timeout_seconds=timeout_seconds if timeout_seconds is not None else number("TRI_RUN_TIMEOUT"),
            cpu_seconds=int(cpu) if cpu else None,
            memory_mb=int(memory) if memory else None,
            file_size_mb=int(file_size) if file_size else None,
            pids=int(pids) if pids else None,
            cpus=cpus or None,
        )

    def describe(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "cpu_seconds": self.cpu_seconds, "memory_mb": self.memory_mb, "file_size_mb": self.file_size_mb, "pids": self.pids, "cpus": self.cpus}


@dataclass
class Container:
    """How the container backend runs: which engine CLI, OCI runtime, image, and user."""

    engine: str
    runtime: str | None
    image: str
    user: str

    @classmethod
    def from_env(cls, engine: str | None = None) -> "Container":
        return cls(
            engine=engine or resolve_engine() or "docker",
            runtime=(os.environ.get(OCI_RUNTIME_ENV) or "").strip() or None,
            image=(os.environ.get(OCI_IMAGE_ENV) or "").strip() or OCI_DEFAULT_IMAGE,
            user=(os.environ.get(OCI_USER_ENV) or "").strip() or default_container_user(),
        )

    def describe(self) -> dict[str, Any]:
        return {"engine": self.engine, "runtime": self.runtime or "runc", "image": self.image, "user": self.user, "microvm": (self.runtime or "").startswith("kata"), "user_space_kernel": self.runtime == "runsc"}


@dataclass
class Sandbox:
    kind: str  # "bwrap", "seatbelt", "winjob", "container", or "none" (limits and environment only)
    network: bool
    writable: list[Path]
    readable: list[Path] = field(default_factory=list)
    hidden: list[Path] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    container: Container | None = None
    # Caller-named hidden paths (the Tri home, a runner's cache and token file).
    # The Windows backend labels these unreadable; it leaves credential files
    # under the user's home alone.
    relabel: list[Path] = field(default_factory=list)
    # Unresolved spellings of writable and readable paths (macOS /var vs
    # /private/var): a container binds both so environment paths resolve.
    aliases: dict[Path, Path] = field(default_factory=dict)

    @property
    def isolated(self) -> bool:
        return self.kind in ISOLATED_KINDS

    @property
    def network_enforced(self) -> bool:
        """Whether a network denial is enforced (Windows cannot without admin rights)."""
        return self.kind in ("bwrap", "seatbelt", "container")

    def describe(self) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "backend": self.kind,
            "isolated": self.isolated,
            "network": self.network,
            "network_enforced": self.network_enforced,
            "writable": [str(path) for path in self.writable],
            "hidden_paths": len(self.hidden),
            "limits": self.limits.describe(),
        }
        if self.container is not None:
            data["container"] = self.container.describe()
            data["runtime"] = self.container.runtime or "runc"
        return data

    def wrap(self, argv: list[str], cwd: Path, *, name: str | None = None) -> list[str]:
        if self.kind == "bwrap":
            return self._bwrap(argv, cwd)
        if self.kind == "seatbelt":
            return ["sandbox-exec", "-p", self._seatbelt_profile(), *argv]
        if self.kind == "container":
            return self._container(argv, cwd, name=name or container_name(), env={})[0]
        if self.kind == "winjob":
            from tri import winsandbox

            return winsandbox.helper_argv(argv)
        return list(argv)

    def _container(self, argv: list[str], cwd: Path, *, name: str, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        """The engine command line, and the environment the engine CLI runs with.

        Values reach the container through the engine's own environment
        (`--env NAME`), never the command line, so a leased secret does not
        show up in `ps`. Paths the engine CLI needs (HOME, PATH) are passed
        inline because they are not secret and differ from the CLI's own."""
        box = self.container or Container.from_env()
        cmd = [box.engine, "run", "--rm", "--name", name, "--label", "tri.sandbox=run"]
        if box.runtime:
            cmd += ["--runtime", box.runtime]
        cmd += ["--network", "bridge" if self.network else "none"]
        cmd += ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", box.user]
        limits = self.limits
        cmd += ["--pids-limit", str(limits.pids or OCI_DEFAULT_PIDS)]
        if limits.memory_mb:
            cmd += ["--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m"]
        if limits.cpus:
            cmd += ["--cpus", f"{limits.cpus:g}"]
        if limits.cpu_seconds:
            cmd += ["--ulimit", f"cpu={limits.cpu_seconds}:{limits.cpu_seconds + 5}"]
        if limits.file_size_mb:
            cmd += ["--ulimit", f"fsize={limits.file_size_mb * 1024 * 1024}"]
        cmd += ["--ulimit", "core=0"]

        def binds(paths: list[Path]) -> list[Path]:
            out: list[Path] = []
            for path in paths:
                for form in (path, self.aliases.get(path)):
                    if form is not None and form not in out:
                        out.append(form)
            return out

        for path in binds(self.readable):
            if path.exists():
                cmd += ["--mount", f"type=bind,source={os.path.realpath(path)},target={path},readonly"]
        for path in binds(self.writable):
            cmd += ["--mount", f"type=bind,source={os.path.realpath(path)},target={path}"]
        cmd += ["--workdir", str(cwd)]
        engine_env = dict(os.environ)
        for key, value in sorted(env.items()):
            if key == "PATH":
                value = OCI_PATH
            if key in OCI_INLINE_ENV:
                cmd += ["--env", f"{key}={value}"]
            else:
                engine_env[key] = value
                cmd += ["--env", key]
        if "PATH" not in env:
            cmd += ["--env", f"PATH={OCI_PATH}"]
        cmd += ["--entrypoint", argv[0], box.image, *argv[1:]] if argv else [box.image]
        return cmd, engine_env

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
        if self.kind == "container":
            # The engine CLI is a Go binary that reserves far more address space
            # than the job's memory limit; the container's own cgroup limits apply.
            return os.setsid

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
    try:
        return _run(sandbox, argv, cwd=cwd, env=env)
    except OSError as exc:  # command not found or not executable: report it like a shell would
        return Completed(127, b"", f"[tri] cannot start {argv[0] if argv else 'command'}: {exc}\n".encode())


def _run(sandbox: Sandbox | None, argv: list[str], *, cwd: Path, env: dict[str, str]) -> Completed:
    if sandbox is None:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)
        return Completed(proc.returncode, proc.stdout, proc.stderr)
    if sandbox.kind == "container":
        return _run_container(sandbox, argv, cwd=cwd, env=env)
    if sandbox.kind == "winjob":
        from tri import winsandbox

        return winsandbox.run(sandbox, argv, cwd=cwd, env=env)
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


def container_name() -> str:
    import uuid

    return f"tri-run-{uuid.uuid4().hex[:16]}"


def _run_container(sandbox: Sandbox, argv: list[str], *, cwd: Path, env: dict[str, str]) -> Completed:
    name = container_name()
    cmd, engine_env = sandbox._container(argv, cwd, name=name, env=env)
    engine = cmd[0]
    kwargs: dict[str, Any] = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": engine_env}
    if os.name != "nt":
        kwargs["preexec_fn"] = os.setsid
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=sandbox.limits.timeout_seconds)
        if proc.returncode in (125, 126, 127) and b"Error response from daemon" in stderr:
            stderr += f"\n[tri] the {engine} engine could not start the container\n".encode()
        return Completed(proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        # Killing the CLI leaves the container running; kill it through the engine.
        remove_container(engine, name)
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        return Completed(-9, stdout, stderr + f"\n[tri] killed after {sandbox.limits.timeout_seconds:g} s wall-clock limit\n".encode(), timed_out=True)
    finally:
        # A CLI that died or an engine error (125) can leave a created container behind.
        if proc.returncode is None or proc.returncode < 0 or proc.returncode == 125:
            remove_container(engine, name)


def remove_container(engine: str, name: str) -> None:
    """Kill and remove a run container; quiet when it is already gone."""
    for action in (["kill", name], ["rm", "-f", name]):
        try:
            subprocess.run([engine, *action], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


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


_PROBES: dict[tuple[str, ...], dict[str, Any]] = {}


def resolve_backend(requested: str | None = None) -> str:
    backend = (requested or os.environ.get(BACKEND_ENV) or "auto").strip().lower()
    return backend if backend in BACKENDS else "auto"


def resolve_engine() -> str | None:
    wanted = (os.environ.get(OCI_ENGINE_ENV) or "").strip()
    if wanted:
        return wanted
    for engine in ("docker", "podman"):
        if shutil.which(engine):
            return engine
    return None


def default_container_user() -> str:
    """The host user's ids, so the bound run directory stays writable; nobody when the host runs as root."""
    if os.name == "nt" or os.getuid() == 0:
        return "65534:65534"
    return f"{os.getuid()}:{os.getgid()}"


def probe() -> dict[str, Any]:
    """Which isolation this host supports for the configured backend; cached per process and configuration."""
    backend = resolve_backend()
    key = (backend, *(os.environ.get(name, "") for name in (OCI_ENGINE_ENV, OCI_RUNTIME_ENV, OCI_IMAGE_ENV, OCI_USER_ENV)))
    if key not in _PROBES:
        result = _probe(backend)
        result.setdefault("backend", result["kind"] if result["kind"] != "none" else backend)
        result.setdefault("requested_backend", backend)
        result.setdefault("network_enforced", result["kind"] in ("bwrap", "seatbelt", "container"))
        _PROBES[key] = result
    return _PROBES[key]


def _probe(backend: str) -> dict[str, Any]:
    if backend == "auto":
        if sys.platform.startswith("linux"):
            backend = "bwrap"
        elif sys.platform == "darwin":
            backend = "seatbelt"
        elif os.name == "nt":
            backend = "winjob"
        else:
            return {"kind": "none", "isolated": False, "reason": f"no sandbox backend for {sys.platform}"}
    if backend == "container":
        return probe_container()
    if backend == "winjob":
        if os.name != "nt":
            return {"kind": "none", "isolated": False, "reason": "the winjob backend needs Windows"}
        from tri import winsandbox

        return winsandbox.probe()
    result: dict[str, Any] = {"kind": "none", "isolated": False, "reason": None}
    if backend == "bwrap":
        if not sys.platform.startswith("linux"):
            result["reason"] = "bubblewrap needs Linux"
        elif shutil.which("bwrap") is None:
            result["reason"] = "bubblewrap (bwrap) is not installed"
        else:
            check = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-net", "--unshare-pid", "--proc", "/proc", "true"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if check.returncode == 0:
                result = {"kind": "bwrap", "isolated": True, "reason": None}
            else:
                result["reason"] = "bwrap cannot create namespaces here: " + check.stderr.decode(errors="replace").strip()[:200]
    elif backend == "seatbelt":
        if sys.platform != "darwin":
            result["reason"] = "sandbox-exec needs macOS"
        elif shutil.which("sandbox-exec") is None:
            result["reason"] = "sandbox-exec is not available"
        else:
            check = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)", "true"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if check.returncode == 0:
                result = {"kind": "seatbelt", "isolated": True, "reason": None}
            else:
                result["reason"] = "sandbox-exec refused a trivial profile: " + check.stderr.decode(errors="replace").strip()[:200]
    return result


def probe_container() -> dict[str, Any]:
    """Start one throwaway container with the configured engine, runtime, and image."""
    engine = resolve_engine()
    if engine is None or shutil.which(engine) is None:
        return {"kind": "none", "isolated": False, "backend": "container", "reason": "neither docker nor podman is installed"}
    if os.name == "nt":
        return {"kind": "none", "isolated": False, "backend": "container", "reason": "the container backend binds POSIX run paths; use winjob on Windows"}
    box = Container.from_env(engine)
    info = box.describe()
    base = {"kind": "none", "isolated": False, "backend": "container", "container": info, "engine": engine, "runtime": info["runtime"]}
    try:
        version = subprocess.run([engine, "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**base, "reason": f"{engine} did not answer: {exc}"}
    if version.returncode != 0:
        return {**base, "reason": f"{engine} cannot reach its daemon: " + version.stderr.decode(errors="replace").strip()[:200]}
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tri-oci-probe-") as tmp:
        root = Path(tmp)
        sandbox = Sandbox(kind="container", network=False, writable=[Path(os.path.realpath(root))], container=box, limits=Limits(timeout_seconds=300))
        os.chmod(root, 0o777)
        completed = _run_container(sandbox, ["sh", "-c", "echo ok > probe.txt"], cwd=Path(os.path.realpath(root)), env={})
        wrote = (root / "probe.txt").exists()
    if completed.returncode != 0 or not wrote:
        detail = completed.stderr.decode(errors="replace").strip()[-300:]
        return {**base, "reason": f"{engine} could not run {box.image} with runtime {info['runtime']}: {detail}"}
    return {**base, "kind": "container", "isolated": True, "reason": None}


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
    named = [Path(os.path.realpath(path)) for path in hidden]
    hidden_paths = named + [Path(os.path.realpath(home / rel)) for rel in SECRET_HOME_PATHS if (home / rel).exists()]
    writable = [Path(os.path.realpath(run_dir))]
    readable_paths = [Path(os.path.realpath(path)) for path in readable]
    aliases = {Path(os.path.realpath(path)): Path(os.path.abspath(path)) for path in [run_dir, *readable] if os.path.realpath(path) != os.path.abspath(path)}
    kind = info["kind"]
    return Sandbox(
        kind=kind,
        network=network,
        writable=writable,
        readable=readable_paths,
        hidden=hidden_paths,
        limits=limits or Limits(),
        container=Container.from_env(info.get("engine")) if kind == "container" else None,
        relabel=named,
        aliases=aliases,
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

