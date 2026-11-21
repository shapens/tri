"""`tri worker --stdio`: the Python side of the native front server.

`native/tri-server` terminates HTTP, verifies bearer tokens, and hands each
request to one of a pool of these worker processes. A worker keeps one Python
interpreter and one open Store for its whole life, and answers requests with the
same `TriAPIHandler` code that `tri serve` runs, so routing, policy, actor
pinning, the server-path refusal, and API-blocked commands are all decided here,
in Python, exactly as they are behind `tri serve`.

Protocol (version 1). Every frame is one JSON header line followed by exactly
``length`` bytes of body (``length`` may be 0):

    worker -> server  {"type": "hello", "protocol": 1, "pid", "schema_version", "home", "auth", "startup", ...}
                      {"type": "fatal", "length": N}  + a tri.error envelope, then exit 1
    server -> worker  {"type": "request", "id", "method", "path", "headers", "actor", "length": N} + body
    worker -> server  {"type": "response", "id", "status", "content_type", "length": N, "wall_ms"} + body
    server -> worker  {"type": "maintain", "id"}   (reap leases and trim caches, as `tri serve` does every 5 minutes)
    worker -> server  {"type": "maintained", "id", "ok", "length": 0}
    server -> worker  {"type": "shutdown"} or end of input: close the store and exit 0

``actor`` is the actor the server authenticated from a bearer token, or null
when the server runs without a token file. The worker never sees the token.

The protocol owns the worker's original stdin and stdout. File descriptor 0 is
replaced with the null device and descriptor 1 with stderr, so a command (or a
subprocess it starts) that reads stdin or prints cannot corrupt the frames.
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import sys
import time
import traceback
from typing import Any, BinaryIO

from tri import cli

PROTOCOL_VERSION = 1


class PinnedAuthority:
    """Stands in for TokenAuthority: the front server has already verified the
    token, so every request carries the actor it resolved to."""

    def __init__(self, actor: str | None) -> None:
        self.actor = actor

    def actor_for(self, token: str | None) -> str | None:
        return self.actor


class WorkerServer:
    """The attributes of TriHTTPServer that TriAPIHandler reads."""

    def __init__(self, host: str, port: int) -> None:
        self.server_address = (host, port)
        self.authority: PinnedAuthority | None = None


class WorkerRequest(cli.TriAPIHandler):
    """One API request, run through TriAPIHandler without a socket."""

    def __init__(self, server: WorkerServer, store: cli.Store, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        # BaseHTTPRequestHandler.__init__ would read a socket; set what the handler uses.
        self.server = server  # type: ignore[assignment]
        self.shared_store = store
        self.command = method
        self.path = path
        message = http.client.HTTPMessage()
        for name, value in headers.items():
            message[name] = value
        self.headers = message
        self.rfile = io.BytesIO(body)
        self.response: tuple[int, bytes] | None = None

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        self.response = (status, cli.response_json(payload).encode())

    def handle_one(self) -> tuple[int, bytes]:
        if self.command == "GET":
            self.do_GET()
        elif self.command == "POST":
            self.do_POST()
        else:
            raise cli.TriError("invalid_command", "Unsupported method.", object=self.command)
        if self.response is None:
            raise RuntimeError("handler wrote no response")
        return self.response


class FrameError(Exception):
    pass


def read_frame(stream: BinaryIO) -> tuple[dict[str, Any], bytes] | None:
    line = stream.readline()
    if not line:
        return None
    try:
        header = json.loads(line)
    except json.JSONDecodeError as exc:
        raise FrameError(f"bad frame header: {exc.msg}") from exc
    if not isinstance(header, dict):
        raise FrameError("frame header must be an object")
    length = int(header.get("length") or 0)
    body = stream.read(length) if length else b""
    if len(body) != length:
        raise FrameError("truncated frame body")
    return header, body


def write_frame(stream: BinaryIO, header: dict[str, Any], body: bytes = b"") -> None:
    header = {**header, "length": len(body)}
    stream.write(json.dumps(header, sort_keys=True, separators=(",", ":")).encode() + b"\n" + body)
    stream.flush()


def claim_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Take the original stdin/stdout for frames; point fds 0 and 1 elsewhere."""
    sys.stdout.flush()
    proto_in = os.fdopen(os.dup(0), "rb", buffering=1 << 16)
    proto_out = os.fdopen(os.dup(1), "wb", buffering=1 << 16)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull, "r")
    sys.stdout = sys.stderr
    return proto_in, proto_out


def end_request(store: cli.Store) -> None:
    """Leave the shared store as a fresh one would be: no open transaction and
    no content cached from this request."""
    if store.conn.in_transaction:
        store.conn.rollback()
    for name in ("_blob_cache", "_tree_cache"):
        cache = getattr(store, name, None)
        if isinstance(cache, dict):
            cache.clear()


def maintain(store: cli.Store) -> None:
    """What `tri serve` does in its reaper thread and after every command."""
    if os.environ.get("TRI_AUTO_REAP", "1") == "0":
        return
    cli.reap_leases(store, actor=cli.REAPER_ACTOR, limit=50)
    cli.evict_caches(store)
    cli.maybe_auto_reap(store)


def serve_worker(store: cli.Store, args: argparse.Namespace, *, command: list[str], start: float) -> dict[str, Any]:
    proto_in, proto_out = claim_stdio()
    try:
        auth_file = cli.configure_serve_runtime(args)
    except cli.TriError as exc:
        failure = cli.envelope("tri.error", command, ok=False, actor=args.actor, start=start, diagnostics=[exc.diagnostic])
        write_frame(proto_out, {"type": "fatal"}, cli.response_json(failure).encode())
        return failure
    server = WorkerServer(args.host, args.port)
    authenticated = bool(auth_file)
    startup_data = {**cli.api_server_data(args.host, args.port), "auth": "bearer" if authenticated else "none", "run_sandbox": os.environ[cli.sandbox.MODE_ENV]}
    startup = cli.envelope("tri.api.server", ["tri-server", "--host", args.host, "--port", str(args.port)], actor=args.actor, start=start, data=startup_data)
    write_frame(
        proto_out,
        {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "schema_version": cli.SCHEMA_VERSION,
            "home": str(store.home),
            "auth": "bearer" if authenticated else "none",
            "run_sandbox": os.environ[cli.sandbox.MODE_ENV],
            "commands": sorted({contract["name"].split("-")[0] for contract in cli.COMMAND_CONTRACTS}),
            "startup": startup,
        },
    )
    handled = 0
    while True:
        try:
            frame = read_frame(proto_in)
        except FrameError as exc:
            print(f"tri worker: {exc}", file=sys.stderr)
            break
        if frame is None:
            break
        header, body = frame
        kind = header.get("type")
        if kind == "shutdown":
            break
        if kind == "maintain":
            ok = True
            try:
                maintain(store)
            except Exception:  # maintenance never takes a worker down
                ok = False
                traceback.print_exc()
            end_request(store)
            write_frame(proto_out, {"type": "maintained", "id": header.get("id"), "ok": ok})
            continue
        if kind != "request":
            print(f"tri worker: unknown frame type {kind!r}", file=sys.stderr)
            break
        began = time.perf_counter()
        server.authority = PinnedAuthority(header.get("actor")) if authenticated else None
        headers = header.get("headers") or {}
        try:
            request = WorkerRequest(server, store, str(header.get("method", "GET")), str(header.get("path", "/")), {str(k): str(v) for k, v in headers.items()}, body)
            status, payload = request.handle_one()
        except Exception as exc:
            # `tri serve` would drop the connection; answer with an envelope instead.
            traceback.print_exc()
            diagnostic = cli.TriError("worker_failed", "The request failed inside the worker.", error=type(exc).__name__).diagnostic
            status, payload = 500, cli.response_json(cli.envelope("tri.error", ["tri", "serve", "request"], ok=False, actor=header.get("actor") or "actor_local", start=time.time(), diagnostics=[diagnostic])).encode()
        try:
            end_request(store)
        except Exception:
            traceback.print_exc()
            write_frame(proto_out, {"type": "response", "id": header.get("id"), "status": status, "content_type": "application/json", "wall_ms": round((time.perf_counter() - began) * 1000, 3), "retire": True}, payload)
            break
        write_frame(proto_out, {"type": "response", "id": header.get("id"), "status": status, "content_type": "application/json", "wall_ms": round((time.perf_counter() - began) * 1000, 3)}, payload)
        handled += 1
    return cli.envelope("tri.worker", command, actor=args.actor, start=start, data={"protocol": PROTOCOL_VERSION, "requests": handled})
