"""PostgreSQL metadata backend that behaves like Python's ``sqlite3`` module.

Tri's store code is written against ``sqlite3`` in its legacy transaction mode:
reads outside a transaction see the latest commit, the first write opens a
transaction that excludes every other writer until commit, and a failed
statement does not abort the surrounding transaction. ``PgConnection`` keeps
exactly those semantics on PostgreSQL so every node of a multi-node deployment
can share one database:

- the first write of a transaction takes a transaction-scoped advisory lock
  for the store, so writers are serialized across processes *and* machines just
  as SQLite serializes writers on one machine;
- every statement inside a transaction runs under a savepoint, so an
  ``IntegrityError`` can be caught and the transaction continued;
- SQL is translated from the SQLite dialect the store uses (``?`` parameters,
  ``INSERT OR IGNORE``, ``PRAGMA table_info``, scalar ``MAX``, implicit
  ``rowid``), and errors are raised as ``sqlite3`` exception subclasses so the
  existing ``except sqlite3.IntegrityError`` handlers keep working.

Each store lives in its own schema. Connections are pooled per process.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from typing import Any, Iterable, Iterator

from tri.pgwire import PgConnection as WireConnection
from tri.pgwire import PgConnectionError, PgError, Result, redact_dsn

DATABASE_URL_ENV = "TRI_DATABASE_URL"
DATABASE_SCHEMA_ENV = "TRI_DATABASE_SCHEMA"
LOCK_NAMESPACE = 0x7472  # "tr" — store-wide lock (shared by scoped writers, exclusive for global ops)
SCOPE_LOCK_NAMESPACE = 0x7473  # "ts" — per-resource lock (workspace, target, changeset, ...)
WRITE_LOCK_TIMEOUT = os.environ.get("TRI_DATABASE_LOCK_TIMEOUT", "120s")


class PgIntegrityError(sqlite3.IntegrityError):
    sqlstate = ""


class PgOperationalError(sqlite3.OperationalError):
    sqlstate = ""


class PgDatabaseError(sqlite3.DatabaseError):
    sqlstate = ""


def translate_error(exc: Exception) -> sqlite3.Error:
    if isinstance(exc, PgConnectionError):
        error: sqlite3.Error = PgOperationalError(f"database connection failed: {exc}")
        error.sqlstate = "08006"  # type: ignore[attr-defined]
        return error
    assert isinstance(exc, PgError)
    state = exc.sqlstate
    message = exc.fields.get("M", str(exc))
    if state.startswith("23"):
        error = PgIntegrityError(message)
    elif state in {"40001", "40P01", "55P03", "57014"}:
        error = PgOperationalError(f"database is locked: {message}")
    elif state.startswith(("42", "08", "53", "57", "58")):
        error = PgOperationalError(message)
    else:
        error = PgDatabaseError(message)
    error.sqlstate = state  # type: ignore[attr-defined]
    return error


# -- SQL translation ------------------------------------------------------------

_TYPE_WORDS = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION", "BLOB": "BYTEA"}
_SCALAR_MAX = re.compile(r"\bMAX\(\s*([A-Za-z_][A-Za-z_0-9]*)\s*,", re.IGNORECASE)
_SCALAR_MIN = re.compile(r"\bMIN\(\s*([A-Za-z_][A-Za-z_0-9]*)\s*,", re.IGNORECASE)
_PARAM_IS_NULL = re.compile(r"\?(\s+IS\s+(?:NOT\s+)?NULL)", re.IGNORECASE)
_EMPTY_IN = re.compile(r"\bIN\s*\(\s*\)", re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_CREATE_TABLE = re.compile(r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[A-Za-z_][A-Za-z_0-9]*\s*\()", re.IGNORECASE)
_PRAGMA_TABLE_INFO = re.compile(r"^\s*PRAGMA\s+table_info\(\s*([A-Za-z_][A-Za-z_0-9]*)\s*\)\s*;?\s*$", re.IGNORECASE)
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
_TYPE_PATTERN = re.compile(r"\b(INTEGER|REAL|BLOB)\b")


def _split_code(sql: str) -> list[tuple[bool, str]]:
    """Split into (is_code, text) segments, leaving string literals and quoted identifiers alone."""
    parts: list[tuple[bool, str]] = []
    buffer: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in "'\"":
            if buffer:
                parts.append((True, "".join(buffer)))
                buffer = []
            end = index + 1
            while end < len(sql):
                if sql[end] == char:
                    if end + 1 < len(sql) and sql[end + 1] == char:
                        end += 2
                        continue
                    break
                end += 1
            parts.append((False, sql[index : end + 1]))
            index = end + 1
            continue
        buffer.append(char)
        index += 1
    if buffer:
        parts.append((True, "".join(buffer)))
    return parts


def _map_code(sql: str, fn) -> str:
    return "".join(fn(text) if is_code else text for is_code, text in _split_code(sql))


def translate_ddl(sql: str) -> str:
    def convert(text: str) -> str:
        text = _TYPE_PATTERN.sub(lambda m: _TYPE_WORDS[m.group(1)], text)
        # SQLite tables carry an implicit, insertion-ordered rowid the store orders by.
        return _CREATE_TABLE.sub(lambda m: m.group(1) + "\n  rowid BIGINT GENERATED BY DEFAULT AS IDENTITY,", text)

    return _map_code(sql, convert)


def translate(sql: str) -> str:
    stripped = sql.lstrip().upper()
    if stripped.startswith(("CREATE", "ALTER")):
        sql = translate_ddl(sql)
    ignore = bool(_INSERT_OR_IGNORE.match(sql))
    if ignore:
        sql = _INSERT_OR_IGNORE.sub("INSERT INTO", sql, count=1)
    counter = [0]

    def convert(text: str) -> str:
        text = _SCALAR_MAX.sub(r"GREATEST(\1,", text)
        text = _SCALAR_MIN.sub(r"LEAST(\1,", text)
        text = _PARAM_IS_NULL.sub(r"CAST(? AS TEXT)\1", text)
        text = _EMPTY_IN.sub("IN (SELECT NULL WHERE FALSE)", text)

        def number(_: re.Match[str]) -> str:
            counter[0] += 1
            return f"${counter[0]}"

        return re.sub(r"\?", number, text)

    sql = _map_code(sql, convert)
    if ignore:
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql


def _selects_rowid(sql: str) -> bool:
    match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, re.IGNORECASE | re.DOTALL)
    return bool(match and re.search(r"\browid\b", match.group(1), re.IGNORECASE))


# -- rows and cursors -----------------------------------------------------------


class Row:
    """A ``sqlite3.Row`` look-alike: index by position or (case-insensitive) name."""

    __slots__ = ("_columns", "_values", "_index")

    def __init__(self, columns: tuple[str, ...], index: dict[str, int], values: tuple[Any, ...]) -> None:
        self._columns = columns
        self._index = index
        self._values = values

    def keys(self) -> list[str]:
        return list(self._columns)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._index[key.lower()]]
            except KeyError:
                raise IndexError("No item with that key") from None
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Row) and self._columns == other._columns and self._values == other._values

    def __hash__(self) -> int:
        return hash((self._columns, self._values))

    def __repr__(self) -> str:
        return f"<Row {dict(zip(self._columns, self._values))!r}>"


class Cursor:
    def __init__(self, result: Result | None = None, *, hide_rowid: bool = False, rowcount: int | None = None) -> None:
        columns = list(result.columns) if result else []
        rows = list(result.rows) if result else []
        if hide_rowid and "rowid" in columns:
            position = columns.index("rowid")
            columns.pop(position)
            rows = [row[:position] + row[position + 1 :] for row in rows]
        self._columns = tuple(columns)
        self._index = {name.lower(): i for i, name in reversed(list(enumerate(columns)))}
        self._rows = rows
        self._position = 0
        self.rowcount = rowcount if rowcount is not None else (result.rowcount if result and not result.columns else -1)
        self.lastrowid = None
        self.description = tuple((name, None, None, None, None, None, None) for name in columns) or None

    def _row(self, values: tuple[Any, ...]) -> Row:
        return Row(self._columns, self._index, values)

    def fetchone(self) -> Row | None:
        if self._position >= len(self._rows):
            return None
        self._position += 1
        return self._row(self._rows[self._position - 1])

    def fetchall(self) -> list[Row]:
        rows = [self._row(values) for values in self._rows[self._position :]]
        self._position = len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list[Row]:
        rows = [self._row(values) for values in self._rows[self._position : self._position + size]]
        self._position += len(rows)
        return rows

    def __iter__(self) -> Iterator[Row]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        self._rows = []


# -- connection -----------------------------------------------------------------

_WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def lock_key(schema: str) -> int:
    return int.from_bytes(hashlib.sha256(schema.encode()).digest()[:4], "big", signed=True)


def scope_lock_key(schema: str, scope: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{schema}\0{scope}".encode()).digest()[:4], "big", signed=True)


class PgConnection:
    """A ``sqlite3.Connection`` stand-in bound to one schema."""

    def __init__(self, url: str, schema: str) -> None:
        self.url = url
        self.schema = schema
        self.row_factory = None  # accepted for compatibility; rows are always Row
        self._write_key = lock_key(schema)
        self._write_scopes: list[tuple[str, ...]] = []
        self._translations: OrderedDict[str, tuple[str, bool]] = OrderedDict()
        self._wire = self._open()
        self.in_transaction = False
        self.closed = False

    @contextlib.contextmanager
    def scoped_lock(self, *scopes: str) -> Iterator[None]:
        """Serialize writes only against matching scopes (plus global store ops).

        Transactions started inside ``scoped_lock`` acquire a *shared* store-wide
        advisory lock and an *exclusive* advisory lock per scope (sorted by key
        to prevent deadlocks). Unscoped transactions (GC, schema migrations,
        backups, global operations) acquire the *exclusive* store-wide lock and
        therefore wait for active scoped writers without blocking unrelated
        workspaces or targets from writing concurrently."""
        cleaned = tuple(sorted({scope for scope in scopes if scope}))
        self._write_scopes.append(cleaned)
        try:
            yield
        finally:
            self._write_scopes.pop()

    def _begin_statements(self) -> list[tuple[str, tuple[Any, ...]]]:
        active_scopes = {scope for group in self._write_scopes for scope in group}
        if active_scopes:
            keys = sorted({scope_lock_key(self.schema, scope) for scope in active_scopes})
            statements: list[tuple[str, tuple[Any, ...]]] = [
                ("BEGIN", ()),
                ("SELECT pg_advisory_xact_lock_shared($1::int, $2::int)", (LOCK_NAMESPACE, self._write_key)),
            ]
            for key in keys:
                statements.append(("SELECT pg_advisory_xact_lock($1::int, $2::int)", (SCOPE_LOCK_NAMESPACE, key)))
            return statements
        return [("BEGIN", ()), ("SELECT pg_advisory_xact_lock($1::int, $2::int)", (LOCK_NAMESPACE, self._write_key))]

    def _open(self) -> WireConnection:
        try:
            wire = WireConnection(self.url)
            wire.simple(f"SET lock_timeout = '{WRITE_LOCK_TIMEOUT}'; SET search_path TO {quote_ident(self.schema)}")
        except (PgError, PgConnectionError) as exc:
            raise translate_error(exc) from exc
        return wire

    # transactions mirror sqlite3's legacy (implicit BEGIN before DML) mode
    def _reconnect(self) -> None:
        """Replace a dropped connection (server restart, idle timeout, failover).
        Only called outside a transaction, so no work is lost or repeated."""
        try:
            self._wire.close()
        except Exception:
            pass
        self._wire = self._open()

    def _begin(self) -> None:
        statements = self._begin_statements()
        try:
            try:
                self._wire.pipeline(statements)
            except PgConnectionError:
                self._reconnect()
                self._wire.pipeline(statements)
        except (PgError, PgConnectionError) as exc:
            self._reset_after_error()
            raise translate_error(exc) from exc
        self.in_transaction = True

    def _reset_after_error(self) -> None:
        try:
            if self._wire.transaction_status != "I":
                self._wire.simple("ROLLBACK")
        except (PgError, PgConnectionError):
            pass
        self.in_transaction = False

    def commit(self) -> None:
        if self.in_transaction:
            self.in_transaction = False
            try:
                self._wire.simple("COMMIT")
            except (PgError, PgConnectionError) as exc:
                self._reset_after_error()
                raise translate_error(exc) from exc

    def rollback(self) -> None:
        if self.in_transaction:
            self.in_transaction = False
            try:
                self._wire.simple("ROLLBACK")
            except (PgError, PgConnectionError) as exc:
                raise translate_error(exc) from exc

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def _translate(self, sql: str) -> tuple[str, bool]:
        cached = self._translations.get(sql)
        if cached is None:
            cached = (translate(sql), not _selects_rowid(sql))
            self._translations[sql] = cached
            if len(self._translations) > 2048:
                self._translations.popitem(last=False)
        else:
            self._translations.move_to_end(sql)
        return cached

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Cursor:
        params = tuple(params)
        head = sql.lstrip()[:16].upper()
        if head.startswith("BEGIN"):
            if not self.in_transaction:
                self._begin()
            return Cursor()
        if head.startswith("COMMIT") or head.startswith("END"):
            self.commit()
            return Cursor()
        if head.startswith("ROLLBACK"):
            self.rollback()
            return Cursor()
        pragma = _PRAGMA_TABLE_INFO.match(sql)
        if pragma:
            return self._run(
                "SELECT column_name AS name, data_type AS type FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = $1 ORDER BY ordinal_position",
                (pragma.group(1).lower(),),
                hide_rowid=True,
            )
        if _PRAGMA.match(sql):
            return Cursor()
        if head.startswith(_WRITE_WORDS) and not self.in_transaction:
            self._begin()
        translated, hide_rowid = self._translate(sql)
        return self._run(translated, params, hide_rowid=hide_rowid)

    def _run(self, sql: str, params: tuple[Any, ...], *, hide_rowid: bool) -> Cursor:
        try:
            if self.in_transaction:
                # Statement-level atomicity, like SQLite: a failed statement
                # rolls back only itself and the transaction stays usable.
                results = self._wire.pipeline([("SAVEPOINT tri_statement", ()), (sql, params), ("RELEASE SAVEPOINT tri_statement", ())])
                result = results[1]
            else:
                try:
                    result = self._wire.query(sql, params)
                except PgConnectionError:
                    # Outside a transaction a statement either ran and committed
                    # or never reached the server; reads and DDL are safe to retry.
                    self._reconnect()
                    result = self._wire.query(sql, params)
        except PgError as exc:
            if self.in_transaction:
                try:
                    self._wire.simple("ROLLBACK TO SAVEPOINT tri_statement; RELEASE SAVEPOINT tri_statement")
                except (PgError, PgConnectionError):
                    self._reset_after_error()
            raise translate_error(exc) from exc
        except PgConnectionError as exc:
            self.in_transaction = False
            raise translate_error(exc) from exc
        return Cursor(result, hide_rowid=hide_rowid)

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> Cursor:
        total = 0
        for params in seq_of_params:
            cursor = self.execute(sql, params)
            total += max(cursor.rowcount, 0)
        return Cursor(rowcount=total)

    def executescript(self, script: str) -> Cursor:
        self.commit()
        statements = [statement for statement in script.split(";") if statement.strip()]
        translated = ";\n".join(translate(statement) for statement in statements)
        try:
            self._wire.simple(translated)
        except (PgError, PgConnectionError) as exc:
            self._reset_after_error()
            raise translate_error(exc) from exc
        return Cursor()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.rollback()
        except sqlite3.Error:
            pass
        self._wire.close()

    # -- pooling ----------------------------------------------------------
    def healthy(self) -> bool:
        return not self.closed and not self.in_transaction and self._wire.transaction_status == "I"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_POOL: OrderedDict[int, PgConnection] = OrderedDict()  # idle connections, oldest first
_POOL_LOCK = threading.Lock()
POOL_SIZE = int(os.environ.get("TRI_DATABASE_POOL_SIZE", "16"))


def connect(url: str, schema: str) -> PgConnection:
    with _POOL_LOCK:
        for key, conn in reversed(_POOL.items()):
            if conn.url == url and conn.schema == schema:
                del _POOL[key]
                break
        else:
            conn = None
    if conn is not None and conn.healthy():
        return conn
    if conn is not None:
        conn.close()
    return PgConnection(url, schema)


def release(conn: PgConnection) -> None:
    """Return a connection to the pool (or close it if it is mid-transaction)."""
    if conn.closed:
        return
    conn._write_scopes.clear()
    if not conn.healthy():
        conn.close()
        return
    evicted: list[PgConnection] = []
    with _POOL_LOCK:
        _POOL[id(conn)] = conn
        while len(_POOL) > POOL_SIZE:
            evicted.append(_POOL.popitem(last=False)[1])
    for old in evicted:
        old.close()


def close_pool() -> None:
    with _POOL_LOCK:
        conns = list(_POOL.values())
        _POOL.clear()
    for conn in conns:
        conn.close()


def schema_for(home: str) -> str:
    explicit = os.environ.get(DATABASE_SCHEMA_ENV, "").strip()
    if explicit:
        return explicit
    return "tri_" + hashlib.sha256(home.encode()).hexdigest()[:16]


def ensure_schema(url: str, schema: str) -> None:
    """Create the schema once, serialized across processes."""
    try:
        wire = WireConnection(url)
    except (PgError, PgConnectionError) as exc:
        raise translate_error(exc) from exc
    try:
        key = lock_key("schema:" + schema)
        wire.query("SELECT pg_advisory_lock($1::int, $2::int)", (LOCK_NAMESPACE, key))
        try:
            wire.simple(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}")
        finally:
            wire.query("SELECT pg_advisory_unlock($1::int, $2::int)", (LOCK_NAMESPACE, key))
    except (PgError, PgConnectionError) as exc:
        raise translate_error(exc) from exc
    finally:
        wire.close()


class SessionLock:
    """A cross-machine named lock (a session advisory lock on its own connection)."""

    def __init__(self, url: str, schema: str, name: str) -> None:
        self.url = url
        self.key = lock_key(f"{schema}:{name}")
        self._wire: WireConnection | None = None

    def acquire(self, exclusive: bool = True, blocking: bool = True) -> bool:
        try:
            self._wire = WireConnection(self.url)
            if blocking:
                self._wire.query("SELECT pg_advisory_lock($1::int, $2::int)", (LOCK_NAMESPACE, self.key))
                return True
            got = self._wire.query("SELECT pg_try_advisory_lock($1::int, $2::int)", (LOCK_NAMESPACE, self.key)).rows[0][0]
        except (PgError, PgConnectionError) as exc:
            self.release()
            raise translate_error(exc) from exc
        if not got:
            self.release()
        return bool(got)

    def release(self) -> None:
        if self._wire is not None:
            self._wire.close()  # closing the session drops its advisory locks
            self._wire = None

    def close(self) -> None:
        self.release()


__all__ = [
    "DATABASE_SCHEMA_ENV",
    "DATABASE_URL_ENV",
    "PgConnection",
    "Row",
    "SessionLock",
    "close_pool",
    "connect",
    "ensure_schema",
    "redact_dsn",
    "release",
    "schema_for",
    "translate",
]


# -- copying a store between SQLite and PostgreSQL ------------------------------


def _sqlite_tables(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return [(table, [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]) for table in tables]


def copy_postgres_to_sqlite(url: str, schema: str, target: sqlite3.Connection) -> dict[str, int]:
    """Copy every table of a PostgreSQL store into a SQLite database that already
    has the store schema, from one consistent snapshot, keeping row order."""
    wire = WireConnection(url)
    counts: dict[str, int] = {}
    try:
        wire.simple(f"SET search_path TO {quote_ident(schema)}; BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table, columns in _sqlite_tables(target):
            present = {row[0] for row in wire.query("SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2", (schema, table)).rows}
            if not present:
                continue
            shared = [column for column in columns if column in present]
            select = ", ".join(quote_ident(column) for column in shared)
            order = " ORDER BY rowid" if "rowid" in present else ""
            rows = wire.query(f"SELECT {select} FROM {quote_ident(table)}{order}").rows
            target.execute(f"DELETE FROM {table}")
            if rows:
                target.executemany(f"INSERT INTO {table} ({', '.join(shared)}) VALUES ({', '.join('?' for _ in shared)})", rows)
            counts[table] = len(rows)
        wire.simple("COMMIT")
    finally:
        wire.close()
    target.commit()
    return counts


def copy_sqlite_to_postgres(source: sqlite3.Connection, url: str, schema: str, *, batch: int = 500) -> dict[str, int]:
    """Copy every table of a SQLite store into an initialized PostgreSQL schema in
    one transaction, preserving SQLite rowids (the store orders by them)."""
    wire = WireConnection(url)
    counts: dict[str, int] = {}
    try:
        wire.simple(f"SET search_path TO {quote_ident(schema)}; BEGIN")
        for table, columns in _sqlite_tables(source):
            present = {row[0] for row in wire.query("SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2", (schema, table)).rows}
            if not present:
                continue
            shared = [column for column in columns if column in present]
            with_rowid = "rowid" in present
            names = (["rowid"] if with_rowid else []) + shared
            selected = ", ".join((["rowid"] if with_rowid else []) + shared)
            wire.query(f"DELETE FROM {quote_ident(table)}")
            rows = source.execute(f"SELECT {selected} FROM {table} ORDER BY rowid").fetchall()
            for start in range(0, len(rows), batch):
                chunk = rows[start : start + batch]
                values: list[Any] = []
                groups = []
                for row in chunk:
                    base = len(values)
                    groups.append("(" + ", ".join(f"${base + i + 1}" for i in range(len(names))) + ")")
                    values.extend(tuple(row))
                wire.query(f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(n) for n in names)}) VALUES {', '.join(groups)}", tuple(values))
            if with_rowid and rows:
                wire.query(f"SELECT setval(pg_get_serial_sequence($1, 'rowid'), (SELECT MAX(rowid) FROM {quote_ident(table)}))", (f"{quote_ident(schema)}.{quote_ident(table)}",))
            counts[table] = len(rows)
        wire.simple("COMMIT")
    except BaseException:
        try:
            wire.simple("ROLLBACK")
        except (PgError, PgConnectionError):
            pass
        raise
    finally:
        wire.close()
    return counts
