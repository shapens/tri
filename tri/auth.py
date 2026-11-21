"""Bearer tokens that bind API and MCP clients to one Tri actor.

Without authentication every client of `tri serve` can claim any actor,
including `actor_local`, which holds maintainer and security roles. A token
file maps the SHA-256 of each token to an actor; the server never stores the
token itself. The file is re-read when it changes, so revoking a token does not
need a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_PREFIX = "tri_"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_token_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text() or "{}")
    tokens = payload.get("tokens", []) if isinstance(payload, dict) else []
    return [item for item in tokens if isinstance(item, dict) and isinstance(item.get("sha256"), str) and isinstance(item.get("actor"), str)]


def write_token_file(path: Path, tokens: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps({"tokens": tokens}, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def issue_token(path: Path, actor: str, *, label: str | None = None) -> tuple[str, dict[str, Any]]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    record = {
        "id": "tok_" + secrets.token_hex(6),
        "actor": actor,
        "sha256": token_digest(token),
        "label": label,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tokens = load_token_file(path)
    tokens.append(record)
    write_token_file(path, tokens)
    return token, record


def revoke_tokens(path: Path, selector: str) -> list[dict[str, Any]]:
    """Remove tokens by id or by actor; returns the removed records."""
    tokens = load_token_file(path)
    removed = [item for item in tokens if item.get("id") == selector or item.get("actor") == selector]
    write_token_file(path, [item for item in tokens if item not in removed])
    return removed


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("id", "actor", "label", "created_at")}


class TokenAuthority:
    """Thread-safe token lookup that follows edits to the token file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._by_digest: dict[str, str] = {}

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = (0, 0)
        if stamp == self._stamp:
            return
        self._by_digest = {item["sha256"]: item["actor"] for item in load_token_file(self.path)}
        self._stamp = stamp

    def actor_for(self, token: str | None) -> str | None:
        if not token:
            return None
        digest = token_digest(token)
        with self._lock:
            self._refresh()
            actor = self._by_digest.get(digest)
        return actor
