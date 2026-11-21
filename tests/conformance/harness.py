"""Black-box conformance harness: drive a Tri implementation only through its
contracts (CLI argv -> JSON envelope, and the HTTP API) and validate every
response against the contract files in contracts/v0.

Nothing here imports `tri`. Which implementation runs is chosen by environment:

    TRI_CONFORMANCE_BIN      CLI command, shell-split (default: "<python> -m tri")
    TRI_CONFORMANCE_NATIVE   path to a tri-server binary: the HTTP suites start it
                             (with TRI_CONFORMANCE_BIN + "worker --stdio" as its
                             worker) instead of "<bin> serve"
    TRI_CONFORMANCE_SERVER   full server command template instead, with {host},
                             {port}, and {auth_args} ("--auth-file <file>" or "")
    TRI_CONFORMANCE_URL      an already-running server to test instead of starting
                             one (same machine: fixtures import local directories);
                             TRI_CONFORMANCE_TOKEN is a maintainer token for it,
                             when it requires tokens
    TRI_CONFORMANCE_CONTRACTS  contract directory (default: contracts/v0)
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = Path(os.environ.get("TRI_CONFORMANCE_CONTRACTS") or ROOT / "contracts" / "v0")


# --- Contracts and a small JSON Schema validator ---------------------------------


class Contracts:
    """Loads contract files and validates instances against them. Supports the
    JSON Schema keywords the generated contracts use: type, required,
    properties, items, anyOf, enum, pattern, and relative-file $ref."""

    def __init__(self, directory: Path = CONTRACTS_DIR) -> None:
        self.directory = directory
        self._cache: dict[str, dict[str, Any]] = {}
        self.index = self.load("index.json")
        self.commands = self.load("commands.json")
        self.api = self.load("api.json")
        self.by_name = {item["name"]: item for item in self.commands["commands"]}
        self.blocked = list(self.commands["api_blocked_commands"])
        self.codes = set(self.load("diagnostic.json")["properties"]["code"]["enum"])

    def load(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            self._cache[name] = json.loads((self.directory / name).read_bytes())
        return self._cache[name]

    def has_schema(self, schema_name: str) -> bool:
        return (self.directory / "schemas" / f"{schema_name}.json").exists()

    def validate(self, instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
        errors: list[str] = []
        if "$ref" in schema:
            errors += self.validate(instance, self.load(schema["$ref"]), path)
        expected = schema.get("type")
        if expected is not None and not type_matches(instance, expected):
            return errors + [f"{path}: expected {expected}, got {type(instance).__name__}"]
        if "enum" in schema and instance not in schema["enum"]:
            errors.append(f"{path}: {instance!r} is not one of the contract's values")
        if "pattern" in schema and isinstance(instance, str) and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
        if isinstance(instance, dict):
            for key in schema.get("required", []):
                if key not in instance:
                    errors.append(f"{path}: missing required {key!r}")
            for key, sub in schema.get("properties", {}).items():
                if key in instance:
                    errors += self.validate(instance[key], sub, f"{path}.{key}")
        if isinstance(instance, list) and "items" in schema:
            for index, item in enumerate(instance):
                errors += self.validate(item, schema["items"], f"{path}[{index}]")
        if "anyOf" in schema:
            variants = [self.validate(instance, variant, path) for variant in schema["anyOf"]]
            if all(variants):
                errors.append(f"{path}: matches none of anyOf ({'; '.join(v[0] for v in variants)})")
        return errors

    def envelope_errors(self, envelope: Any, *, summary: bool = False, expect_schema: str | None = None) -> list[str]:
        """Validate a response envelope, its diagnostics, and (full mode) its data."""
        if not isinstance(envelope, dict):
            return [f"envelope is {type(envelope).__name__}, not an object"]
        errors = self.validate(envelope, self.load("envelope-summary.json" if summary else "envelope.json"))
        if errors:
            return errors
        schema_name = envelope["schema"]
        for key in ("diagnostics", "warnings"):
            for index, item in enumerate(envelope.get(key) or []):
                errors += self.validate(item, self.load("diagnostic.json"), f"$.{key}[{index}]")
        if schema_name == "tri.error":
            if envelope["ok"]:
                errors.append("tri.error envelope with ok=true")
            if not envelope.get("diagnostics"):
                errors.append("tri.error envelope without diagnostics")
        elif expect_schema is not None and schema_name != expect_schema:
            errors.append(f"schema {schema_name!r}, contract says {expect_schema!r}")
        if not summary and schema_name != "tri.error":
            if not self.has_schema(schema_name):
                errors.append(f"schema {schema_name!r} has no contract file")
            else:
                errors += self.validate(envelope["data"], self.load(f"schemas/{schema_name}.json"), "$.data")
        return errors

    def command_schema(self, argv: list[str]) -> str | None:
        """The contract schema for an argv, by longest matching command name."""
        words = [word for word in strip_global_flags(argv) if not word.startswith("-")]
        for size in range(min(len(words), 4), 0, -1):
            name = "-".join(words[:size])
            if name in self.by_name:
                return self.by_name[name]["schema"]
        return None


def type_matches(instance: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(type_matches(instance, item) for item in expected)
    return {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "boolean": isinstance(instance, bool),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "null": instance is None,
    }.get(expected, True)


def strip_global_flags(argv: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--actor", "--response", "--remote"):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


# --- Implementations ---------------------------------------------------------------


def cli_command() -> list[str]:
    raw = os.environ.get("TRI_CONFORMANCE_BIN")
    return shlex.split(raw, posix=os.name != "nt") if raw else [sys.executable, "-m", "tri"]


def native_binary() -> str | None:
    return os.environ.get("TRI_CONFORMANCE_NATIVE") or None


def implementation_name() -> str:
    if os.environ.get("TRI_CONFORMANCE_URL"):
        return "url"
    if native_binary():
        return "tri-server"
    if os.environ.get("TRI_CONFORMANCE_SERVER"):
        return "custom"
    return "tri serve"


def server_argv(host: str, port: int, auth_file: str | None = None, *, native: str | None = None) -> list[str]:
    """The command that starts the server under test on host:port."""
    auth_args = ["--auth-file", auth_file] if auth_file else []
    native = native if native is not None else native_binary()
    template = os.environ.get("TRI_CONFORMANCE_SERVER")
    if native:
        return [native, "--host", host, "--port", str(port), "--quiet", "--workers", os.environ.get("TRI_CONFORMANCE_WORKERS", "2"), *auth_args, "--", *cli_command(), "worker", "--stdio"]
    if template:
        filled = template.replace("{host}", host).replace("{port}", str(port)).replace("{auth_args}", shlex.join(auth_args) if auth_args else "")
        return shlex.split(filled, posix=os.name != "nt")
    return [*cli_command(), "serve", "--host", host, "--port", str(port), "--quiet", *auth_args]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Response:
    def __init__(self, status: int, headers: dict[str, str], raw: bytes) -> None:
        self.status = status
        self.headers = headers
        self.raw = raw
        self.text = raw.decode(errors="replace")
        try:
            self.json: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            self.json = None

    def codes(self) -> list[str]:
        if not isinstance(self.json, dict):
            return []
        return [item.get("code") for item in self.json.get("diagnostics") or []]


class Server:
    """A running server under test: one this harness started, or TRI_CONFORMANCE_URL."""

    def __init__(self, url: str, proc: subprocess.Popen | None = None, log: Path | None = None) -> None:
        parsed = urlparse(url)
        self.url = url.rstrip("/")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.proc = proc
        self.log = log

    @classmethod
    def start(cls, env: dict[str, str], auth_file: str | None = None, *, native: str | None = None, cwd: Path | None = None) -> "Server":
        port = free_port()
        log = Path(tempfile.mkstemp(prefix="tri-conformance-server-", suffix=".log")[1])
        handle = open(log, "wb")
        proc = subprocess.Popen(server_argv("127.0.0.1", port, auth_file, native=native), cwd=cwd or ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        handle.close()
        server = cls(f"http://127.0.0.1:{port}", proc, log)
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited with {proc.returncode}: {log.read_text(errors='replace')[-2000:]}")
            try:
                if server.request("GET", "/v0/health", timeout=2).status == 200:
                    return server
            except OSError:
                time.sleep(0.05)
        server.stop()
        raise RuntimeError("server did not answer /v0/health within 60 s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self.log is not None:
            try:
                self.log.unlink()
            except OSError:
                pass

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None, *, timeout: float = 60) -> Response:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            all_headers = {"Content-Type": "application/json", **(headers or {})}
            conn.request(method, path, body=body, headers=all_headers)
            resp = conn.getresponse()
            return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
        finally:
            conn.close()

    def get(self, path: str, token: str | None = None, headers: dict[str, str] | None = None) -> Response:
        return self.request("GET", path, headers={**auth_header(token), **(headers or {})})

    def post(self, path: str, payload: Any, token: str | None = None, headers: dict[str, str] | None = None) -> Response:
        return self.request("POST", path, json.dumps(payload).encode(), headers={**auth_header(token), **(headers or {})})

    def command(self, argv: list[str], token: str | None = None, **extra: Any) -> Response:
        return self.post("/v0/command", {"argv": argv, **extra}, token=token)


def auth_header(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def run_cli(argv: list[str], env: dict[str, str], *, cwd: Path | None = None, stdin: bytes | None = None) -> tuple[int, Any, str]:
    proc = subprocess.run([*cli_command(), *argv], cwd=cwd or ROOT, env=env, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    text = proc.stdout.decode(errors="replace")
    try:
        output = json.loads(text)
    except json.JSONDecodeError:
        output = None
    return proc.returncode, output, proc.stderr.decode(errors="replace")


def make_repo(root: Path) -> Path:
    """A small Git repository written as bytes, so Windows checkouts stay LF."""
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "README.md").write_bytes(b"# conformance fixture\n")
    (repo / "src" / "app.py").write_bytes(b"def main():\n    return 'conformance'\n")
    (repo / "src" / "util.py").write_bytes(b"VALUE = 1\n")
    git = ["git", "-C", str(repo), "-c", "user.email=ci@example.com", "-c", "user.name=Tri Conformance", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
    return repo


def base_env(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRI_") or key.startswith("TRI_CONFORMANCE")}
    env["TRI_HOME"] = str(home)
    env["TRI_AUTO_REAP"] = "0"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    return env
