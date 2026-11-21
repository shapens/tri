"""Regional read-through cache in front of a Tri service.

`tri cache serve --upstream https://tri.example.com` runs near a group of
machines. Every request is forwarded upstream with the caller's own
Authorization header, so the service still authenticates and authorizes
everything. File reads (`files fetch`, `runner fetch`) are split in two: the
proxy asks the service for the authorized hashes only (`--hashes-only`), then
requests content just for hashes its shared content-addressed store lacks.
Content therefore crosses the WAN once per region, and a client can only
receive bytes the service has just authorized it to read.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import sys
import time
import uuid
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

FETCH_COMMANDS = (("files", "fetch"), ("runner", "fetch"))


class CacheStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.hits = 0
        self.misses = 0

    def path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def get(self, digest: str) -> bytes | None:
        try:
            return self.path(digest).read_bytes()
        except OSError:
            return None

    def put(self, digest: str, data: bytes) -> None:
        if hashlib.sha256(data).hexdigest() != digest:
            return
        path = self.path(digest)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)


class RegionalCacheServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    upstream: str = ""
    cache: CacheStore

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class RegionalCacheHandler(http.server.BaseHTTPRequestHandler):
    server: RegionalCacheServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _upstream(self, method: str, path: str, body: bytes | None) -> tuple[int, bytes, str]:
        req = request.Request(self.server.upstream + path, data=body, method=method)
        for header in ("Authorization", "Content-Type", "X-Tri-Actor"):
            value = self.headers.get(header)
            if value:
                req.add_header(header, value)
        try:
            with request.urlopen(req, timeout=900) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "application/json")
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "application/json")
        except (URLError, OSError) as exc:
            payload = {"ok": False, "schema": "tri.error", "data": {}, "events": [], "warnings": [], "diagnostics": [{"code": "remote_unreachable", "severity": "error", "message": f"Regional cache cannot reach the Tri service: {exc}", "retryable": True}]}
            return 502, json.dumps(payload).encode(), "application/json"

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v0/cache/health":
            body = json.dumps({"ok": True, "upstream": self.server.upstream, "hits": self.server.cache.hits, "misses": self.server.cache.misses}).encode()
            return self._reply(200, body)
        self._reply(*self._upstream("GET", self.path, None))

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v0/command":
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = None
            argv = payload.get("argv") if isinstance(payload, dict) else None
            if isinstance(argv, list) and tuple(argv[:2]) in FETCH_COMMANDS and "--hashes-only" not in argv:
                return self._cached_fetch(payload, argv)
        self._reply(*self._upstream("POST", self.path, body))

    def _cached_fetch(self, payload: dict[str, Any], argv: list[str]) -> None:
        status, raw, content_type = self._upstream("POST", "/v0/command", json.dumps({**payload, "argv": [*argv, "--hashes-only"]}).encode())
        if status != 200:
            return self._reply(status, raw, content_type)
        authorized = json.loads(raw)
        if not authorized.get("ok"):
            return self._reply(status, raw, content_type)
        files = authorized["data"]["files"]
        missing = [item["path"] for item in files if self.server.cache.get(item["hash"]) is None]
        fetched: dict[str, bytes] = {}
        if missing:
            base = [part for index, part in enumerate(argv) if part != "--path" and (index == 0 or argv[index - 1] != "--path")]
            pending = list(missing)
            while pending:
                chunk, pending = pending[:256], pending[256:]
                sub_argv = base + sum((["--path", path] for path in chunk), [])
                sub_status, sub_raw, sub_type = self._upstream("POST", "/v0/command", json.dumps({**payload, "argv": sub_argv}).encode())
                sub = json.loads(sub_raw) if sub_status == 200 else None
                if not sub or not sub.get("ok"):
                    return self._reply(sub_status, sub_raw, sub_type)
                for item in sub["data"]["files"]:
                    data = base64.b64decode(item["content"])
                    self.server.cache.put(item["hash"], data)
                    fetched[item["hash"]] = data
                pending = sub["data"].get("deferred", []) + pending
        out = []
        total = 0
        for item in files:
            data = fetched.get(item["hash"])
            if data is None:
                data = self.server.cache.get(item["hash"])
                self.server.cache.hits += 1
            else:
                self.server.cache.misses += 1
            if data is None:
                return self._reply(502, json.dumps({"ok": False, "schema": "tri.error", "data": {}, "events": [], "warnings": [], "diagnostics": [{"code": "content_mismatch", "severity": "error", "message": "Regional cache lost content mid-request.", "retryable": True}]}).encode())
            total += len(data)
            out.append({**item, "size": len(data), "encoding": "base64", "content": base64.b64encode(data).decode()})
        authorized["data"]["files"] = out
        authorized["data"]["bytes"] = total
        authorized["data"]["deferred"] = []
        authorized.setdefault("refs", {})["regional_cache"] = {"hits": len(files) - len(missing), "misses": len(missing)}
        self._reply(200, json.dumps(authorized).encode())


def serve_cache(args: Any, *, command: list[str], start: float) -> dict[str, Any]:
    from tri.cli import envelope, print_json

    cache_dir = Path(args.dir).expanduser() if args.dir else Path.home() / ".cache" / "tri-regional"
    server = RegionalCacheServer((args.host, args.port), RegionalCacheHandler)
    server.upstream = args.upstream.rstrip("/")
    server.cache = CacheStore(cache_dir / "blobs")
    host, port = server.server_address[:2]
    data = {"host": str(host), "port": int(port), "upstream": server.upstream, "dir": str(cache_dir)}
    if not args.quiet:
        print_json(envelope("tri.cache.server", command, actor=args.actor, start=start, data=data))
        sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return envelope("tri.cache.server", command, actor=args.actor, start=start, data=data)
