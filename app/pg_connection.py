"""SQLite-shaped psycopg connection wrapper for PostgresStore."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from threading import RLock, local
from typing import Any

import psycopg
from psycopg.rows import dict_row

try:
    from psycopg.errors import UniqueViolation
except ImportError:  # pragma: no cover - older psycopg
    from psycopg import errors as _errors

    UniqueViolation = _errors.UniqueViolation


class IntegrityError(Exception):
    """Raised for unique-constraint violations (sqlite3.IntegrityError analogue)."""


class OperationalError(Exception):
    """Raised for operational SQL failures (sqlite3.OperationalError analogue)."""


class PostgresRow(Mapping[str, Any]):
    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            values = list(self._data.values())
            return values[key]
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):
        return self._data.keys()


def _adapt_sql(statement: str) -> str:
    adapted = statement
    adapted = re.sub(r"\bINSERT OR REPLACE INTO\b", "INSERT INTO", adapted, flags=re.IGNORECASE)
    adapted = re.sub(r"\bINSERT OR IGNORE INTO\b", "INSERT INTO", adapted, flags=re.IGNORECASE)
    adapted = re.sub(r"\bdatetime\(([^)]+)\)", r"\1", adapted, flags=re.IGNORECASE)
    adapted = re.sub(r",\s*rowid\s+DESC", "", adapted, flags=re.IGNORECASE)
    adapted = re.sub(r"\s+ORDER BY\s+rowid\s+(?:ASC|DESC)\b", "", adapted, flags=re.IGNORECASE)
    adapted = adapted.replace("?", "%s")
    return adapted


class PostgresCursor:
    def __init__(
        self,
        cursor: psycopg.Cursor[dict[str, Any]],
        lock: RLock | None = None,
    ) -> None:
        self._cursor = cursor
        self._lock = lock or RLock()
        self._pending_on_conflict: str | None = None

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> PostgresCursor:
        sql_text = _adapt_sql(statement)
        if "INSERT INTO" in sql_text.upper() and "ON CONFLICT" not in sql_text.upper():
            sql_text, on_conflict = _maybe_add_on_conflict(statement, sql_text)
            if on_conflict:
                sql_text = f"{sql_text} {on_conflict}"
        with self._lock:
            try:
                self._cursor.execute(sql_text, params or ())
            except UniqueViolation as exc:
                raise IntegrityError(str(exc)) from exc
            except psycopg.Error as exc:
                raise OperationalError(str(exc)) from exc
        return self

    def fetchone(self) -> PostgresRow | None:
        with self._lock:
            row = self._cursor.fetchone()
        return PostgresRow(row) if row is not None else None

    def fetchall(self) -> list[PostgresRow]:
        with self._lock:
            return [PostgresRow(row) for row in self._cursor.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


def _maybe_add_on_conflict(original: str, adapted: str) -> tuple[str, str | None]:
    upper = original.upper()
    if "INSERT OR REPLACE" not in upper and "INSERT OR IGNORE" not in upper:
        return adapted, None

    table_match = re.search(
        r"INSERT OR (?:REPLACE|IGNORE) INTO\s+(\w+)",
        original,
        flags=re.IGNORECASE,
    )
    if not table_match:
        return adapted, None
    table = table_match.group(1)

    conflict_targets = {
        "memory_relations": ("from_memory_id, to_memory_id, relation", "DO NOTHING"),
    }
    if table not in conflict_targets:
        return adapted, None

    target, action = conflict_targets[table]
    if action == "DO NOTHING":
        return adapted, f"ON CONFLICT ({target}) DO NOTHING"
    return adapted, f"ON CONFLICT ({target}) DO {action}"


class PostgresConnection:
    """Minimal sqlite3.Connection-compatible wrapper around psycopg."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn
        self._lock = RLock()
        self._local = local()
        self.row_factory = dict_row

    @classmethod
    def connect(cls, database_url: str) -> PostgresConnection:
        conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=True)
        return cls(conn)

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> PostgresCursor:
        with self._lock:
            cursor = self._conn.cursor()
            wrapped = PostgresCursor(cursor, self._lock)
            wrapped.execute(statement, params)
            return wrapped

    def executescript(self, script: str) -> None:
        statements = [chunk.strip() for chunk in script.split(";") if chunk.strip()]
        with self._lock:
            with self._conn.transaction():
                for statement in statements:
                    self._conn.execute(statement)

    def commit(self) -> None:
        with self._lock:
            if not self._conn.autocommit:
                self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            if not self._conn.autocommit:
                self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> PostgresConnection:
        self._lock.acquire()
        transaction = self._conn.transaction()
        try:
            transaction.__enter__()
        except BaseException:
            self._lock.release()
            raise
        stack = getattr(self._local, "transactions", None)
        if stack is None:
            stack = []
            self._local.transactions = stack
        stack.append(transaction)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        stack = getattr(self._local, "transactions", None)
        if not stack:  # pragma: no cover - context-manager misuse
            self._lock.release()
            raise RuntimeError("PostgreSQL transaction context is not active")
        transaction = stack.pop()
        try:
            transaction.__exit__(exc_type, exc, tb)
        finally:
            self._lock.release()
