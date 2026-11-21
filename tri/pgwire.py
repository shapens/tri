"""A small PostgreSQL client speaking protocol 3.0, standard library only.

Supports TCP and Unix sockets, TLS (``sslmode=require`` / ``verify-full``),
trust, cleartext, MD5 and SCRAM-SHA-256 authentication, simple queries (for
scripts) and the extended protocol with cached prepared statements. Values are
exchanged in text format and decoded by type OID.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import ssl
import struct
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

PROTOCOL_VERSION = 196608  # 3.0
SSL_REQUEST = 80877103

INT_OIDS = {20, 21, 23, 26, 28}
FLOAT_OIDS = {700, 701}
NUMERIC_OID = 1700
BOOL_OID = 16
BYTEA_OID = 17
INT8_OID = 20
FLOAT8_OID = 701


class PgError(Exception):
    """An ErrorResponse from the server; ``fields`` holds the decoded fields."""

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields
        self.sqlstate = fields.get("C", "")
        super().__init__(f"{fields.get('S', 'ERROR')}: {fields.get('M', 'unknown error')} (SQLSTATE {self.sqlstate})")


class PgConnectionError(Exception):
    """The connection could not be made or was lost."""


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    tag: str = ""

    @property
    def rowcount(self) -> int:
        parts = self.tag.split()
        if parts and parts[-1].isdigit() and parts[0] in {"INSERT", "UPDATE", "DELETE", "SELECT", "MERGE", "COPY", "FETCH", "MOVE"}:
            return int(parts[-1])
        return -1


def parse_dsn(url: str) -> dict[str, Any]:
    """Parse ``postgres://user:pass@host:port/db?sslmode=...&host=/socket/dir``."""
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("database URL must start with postgres:// or postgresql://")
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    host = unquote(parsed.hostname or "") or query.get("host") or os.environ.get("PGHOST") or "localhost"
    return {
        "host": query.get("host", host),
        "port": int(parsed.port or query.get("port") or os.environ.get("PGPORT") or 5432),
        "user": unquote(parsed.username or "") or os.environ.get("PGUSER") or os.environ.get("USER", "postgres"),
        "password": unquote(parsed.password or "") or query.get("password") or os.environ.get("PGPASSWORD", ""),
        "database": unquote(parsed.path.lstrip("/")) or os.environ.get("PGDATABASE") or "postgres",
        "sslmode": query.get("sslmode", os.environ.get("PGSSLMODE", "prefer")),
        "sslrootcert": query.get("sslrootcert"),
        "connect_timeout": float(query.get("connect_timeout", 10)),
        "application_name": query.get("application_name", "tri"),
    }


def redact_dsn(url: str) -> str:
    parsed = urlparse(url)
    if parsed.password is None:
        return url
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
    return parsed._replace(netloc=netloc).geturl()


def _cstr(value: str) -> bytes:
    return value.encode() + b"\x00"


def _message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def encode_param(value: Any) -> tuple[int, bytes | None]:
    """Return (type oid, text bytes). Strings go as unknown (oid 0) so the server infers."""
    if value is None:
        return 0, None
    if isinstance(value, bool):
        return INT8_OID, b"1" if value else b"0"
    if isinstance(value, int):
        return INT8_OID, str(value).encode()
    if isinstance(value, float):
        return FLOAT8_OID, repr(value).encode()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return BYTEA_OID, b"\\x" + bytes(value).hex().encode()
    return 0, str(value).encode()


def decode_value(oid: int, raw: bytes | None) -> Any:
    if raw is None:
        return None
    if oid in INT_OIDS:
        return int(raw)
    if oid in FLOAT_OIDS:
        return float(raw)
    if oid == NUMERIC_OID:
        text = raw.decode()
        return int(text) if text.lstrip("-").isdigit() else float(text)
    if oid == BOOL_OID:
        return raw == b"t"
    if oid == BYTEA_OID:
        text = raw.decode()
        return bytes.fromhex(text[2:]) if text.startswith("\\x") else raw
    return raw.decode("utf-8")


class PgConnection:
    def __init__(self, url: str) -> None:
        self.dsn = parse_dsn(url)
        self.parameters: dict[str, str] = {}
        self.transaction_status = "I"
        self.notices: list[dict[str, str]] = []
        self._statements: dict[tuple[str, tuple[int, ...]], str] = {}
        self._next_statement = 0
        self._buffer = b""
        self.sock = self._connect()
        try:
            self._startup()
        except Exception:
            self.sock.close()
            raise

    # -- connection -----------------------------------------------------
    def _connect(self) -> socket.socket:
        dsn = self.dsn
        host = dsn["host"]
        try:
            if host.startswith("/"):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(dsn["connect_timeout"])
                sock.connect(os.path.join(host, f".s.PGSQL.{dsn['port']}"))
            else:
                sock = socket.create_connection((host, dsn["port"]), timeout=dsn["connect_timeout"])
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            raise PgConnectionError(f"could not connect to {host}:{dsn['port']}: {exc}") from exc
        sock.settimeout(None)
        mode = dsn["sslmode"]
        if mode == "disable" or host.startswith("/"):
            return sock
        sock.sendall(struct.pack("!II", 8, SSL_REQUEST))
        answer = sock.recv(1)
        if answer == b"S":
            if mode in {"verify-ca", "verify-full"}:
                context = ssl.create_default_context(cafile=dsn["sslrootcert"])
                context.check_hostname = mode == "verify-full"
            else:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return context.wrap_socket(sock, server_hostname=host)
        if mode in {"require", "verify-ca", "verify-full"}:
            sock.close()
            raise PgConnectionError("server does not support TLS but sslmode requires it")
        return sock

    def _startup(self) -> None:
        dsn = self.dsn
        params = b"".join(_cstr(k) + _cstr(v) for k, v in (("user", dsn["user"]), ("database", dsn["database"]), ("application_name", dsn["application_name"]), ("client_encoding", "UTF8")))
        payload = struct.pack("!I", PROTOCOL_VERSION) + params + b"\x00"
        self.sock.sendall(struct.pack("!I", len(payload) + 4) + payload)
        scram: dict[str, Any] = {}
        while True:
            kind, body = self._read_message()
            if kind == b"R":
                code = struct.unpack("!I", body[:4])[0]
                if code == 0:
                    continue
                if code == 3:
                    self._send(_message(b"p", _cstr(dsn["password"])))
                elif code == 5:
                    salt = body[4:8]
                    inner = hashlib.md5((dsn["password"] + dsn["user"]).encode()).hexdigest()
                    outer = hashlib.md5(inner.encode() + salt).hexdigest()
                    self._send(_message(b"p", _cstr("md5" + outer)))
                elif code == 10:
                    mechanisms = [m for m in body[4:].split(b"\x00") if m]
                    if b"SCRAM-SHA-256" not in mechanisms:
                        raise PgConnectionError(f"unsupported SASL mechanisms: {mechanisms}")
                    nonce = base64.b64encode(os.urandom(18)).decode()
                    scram["client_first_bare"] = f"n=,r={nonce}"
                    scram["nonce"] = nonce
                    first = ("n,," + scram["client_first_bare"]).encode()
                    self._send(_message(b"p", _cstr("SCRAM-SHA-256") + struct.pack("!I", len(first)) + first))
                elif code == 11:
                    server_first = body[4:].decode()
                    attrs = dict(item.split("=", 1) for item in server_first.split(","))
                    if not attrs["r"].startswith(scram["nonce"]):
                        raise PgConnectionError("SCRAM nonce mismatch")
                    salted = hashlib.pbkdf2_hmac("sha256", dsn["password"].encode(), base64.b64decode(attrs["s"]), int(attrs["i"]))
                    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
                    stored_key = hashlib.sha256(client_key).digest()
                    without_proof = f"c=biws,r={attrs['r']}"
                    auth_message = f"{scram['client_first_bare']},{server_first},{without_proof}".encode()
                    signature = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
                    proof = bytes(a ^ b for a, b in zip(client_key, signature))
                    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
                    scram["server_signature"] = base64.b64encode(hmac.new(server_key, auth_message, hashlib.sha256).digest()).decode()
                    self._send(_message(b"p", f"{without_proof},p={base64.b64encode(proof).decode()}".encode()))
                elif code == 12:
                    final = dict(item.split("=", 1) for item in body[4:].decode().split(","))
                    if not hmac.compare_digest(final.get("v", ""), scram.get("server_signature", "")):
                        raise PgConnectionError("SCRAM server signature mismatch")
                else:
                    raise PgConnectionError(f"unsupported authentication request {code}")
            elif kind == b"E":
                raise PgError(self._fields(body))
            elif kind == b"Z":
                self.transaction_status = body.decode()
                return
            else:
                self._handle_async(kind, body)

    # -- framing --------------------------------------------------------
    def _send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise PgConnectionError(f"connection lost: {exc}") from exc

    def _recv_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                chunk = self.sock.recv(max(65536, size - len(self._buffer)))
            except OSError as exc:
                raise PgConnectionError(f"connection lost: {exc}") from exc
            if not chunk:
                raise PgConnectionError("server closed the connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _read_message(self) -> tuple[bytes, bytes]:
        header = self._recv_exact(5)
        length = struct.unpack("!I", header[1:])[0]
        return header[:1], self._recv_exact(length - 4)

    @staticmethod
    def _fields(body: bytes) -> dict[str, str]:
        fields: dict[str, str] = {}
        for part in body.split(b"\x00"):
            if part:
                fields[chr(part[0])] = part[1:].decode("utf-8", "replace")
        return fields

    def _handle_async(self, kind: bytes, body: bytes) -> None:
        if kind == b"S":
            name, value = body.split(b"\x00")[:2]
            self.parameters[name.decode()] = value.decode()
        elif kind == b"N":
            self.notices.append(self._fields(body))
            del self.notices[:-20]
        # BackendKeyData (K) and NotificationResponse (A) are ignored.

    def _read_until_ready(self) -> list[Result]:
        results: list[Result] = []
        current = Result()
        oids: list[int] = []
        error: PgError | None = None
        while True:
            kind, body = self._read_message()
            if kind == b"T":
                count = struct.unpack("!H", body[:2])[0]
                offset = 2
                current = Result()
                oids = []
                for _ in range(count):
                    end = body.index(b"\x00", offset)
                    current.columns.append(body[offset:end].decode())
                    offset = end + 1
                    oids.append(struct.unpack("!IHIhih", body[offset : offset + 18])[2])
                    offset += 18
            elif kind == b"D":
                count = struct.unpack("!H", body[:2])[0]
                offset = 2
                values = []
                for index in range(count):
                    size = struct.unpack("!i", body[offset : offset + 4])[0]
                    offset += 4
                    if size < 0:
                        values.append(None)
                    else:
                        values.append(decode_value(oids[index] if index < len(oids) else 25, body[offset : offset + size]))
                        offset += size
                current.rows.append(tuple(values))
            elif kind == b"C":
                current.tag = body.rstrip(b"\x00").decode()
                results.append(current)
                current = Result()
                oids = []
            elif kind == b"I":
                results.append(Result(tag=""))
            elif kind == b"E":
                error = PgError(self._fields(body))
            elif kind == b"Z":
                self.transaction_status = body.decode()
                if error is not None:
                    raise error
                return results
            elif kind in {b"1", b"2", b"3", b"n", b"t", b"s"}:
                continue
            else:
                self._handle_async(kind, body)

    # -- queries --------------------------------------------------------
    def simple(self, sql: str) -> list[Result]:
        self._send(_message(b"Q", _cstr(sql)))
        return self._read_until_ready()

    def query(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Result:
        return self.pipeline([(sql, params)])[-1]

    def pipeline(self, statements: list[tuple[str, tuple[Any, ...] | list[Any]]]) -> list[Result]:
        """Run statements back to back in one round trip (one Sync).

        If one fails the server skips the rest, and the error is raised."""
        out = bytearray()
        new_names: list[tuple[tuple[str, tuple[int, ...]], str]] = []
        for sql, params in statements:
            encoded = [encode_param(value) for value in params]
            type_oids = tuple(oid for oid, _ in encoded)
            key = (sql, type_oids)
            name = self._statements.get(key)
            if name is None:
                name = f"tri_{self._next_statement}"
                self._next_statement += 1
                new_names.append((key, name))
                out += _message(b"P", _cstr(name) + _cstr(sql) + struct.pack("!H", len(type_oids)) + b"".join(struct.pack("!I", oid) for oid in type_oids))
            bind = bytearray(_cstr("") + _cstr(name) + struct.pack("!H", 0) + struct.pack("!H", len(encoded)))
            for _, value in encoded:
                bind += struct.pack("!i", -1) if value is None else struct.pack("!i", len(value)) + value
            bind += struct.pack("!H", 0)
            out += _message(b"B", bytes(bind))
            out += _message(b"D", b"P" + _cstr(""))
            out += _message(b"E", _cstr("") + struct.pack("!I", 0))
        out += _message(b"S", b"")
        self._send(bytes(out))
        # Names are never reused, so statements parsed in a failed exchange
        # cannot collide; only cache them once the exchange succeeds.
        results = self._read_until_ready()
        for key, name in new_names:
            self._statements[key] = name
        return results

    def close(self) -> None:
        try:
            self._send(_message(b"X", b""))
        except PgConnectionError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
