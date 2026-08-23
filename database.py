"""Small psycopg connection-pool wrapper for Xiaxia Reading House."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


_pool: ConnectionPool | None = None
_database_url: str | None = None
_pool_min = 1
_pool_max = 5


def init_app(app: Any) -> None:
    """Record database settings without forcing a connection at import time."""
    global _database_url, _pool_min, _pool_max
    _database_url = app.config.get("DATABASE_URL") or None
    _pool_min = int(app.config.get("DB_POOL_MIN", 1))
    _pool_max = int(app.config.get("DB_POOL_MAX", 5))


def _get_pool() -> ConnectionPool:
    global _pool
    if not _database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_database_url,
            min_size=_pool_min,
            max_size=_pool_max,
            open=True,
            kwargs={"row_factory": dict_row},
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


atexit.register(close_pool)


@contextmanager
def transaction() -> Iterator[Any]:
    """Yield one connection inside a committed-or-rolled-back transaction."""
    with _get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def fetch_all(query: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    with _get_pool().connection() as conn:
        return list(conn.execute(query, params or ()).fetchall())


def fetch_one(query: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    with _get_pool().connection() as conn:
        return conn.execute(query, params or ()).fetchone()


def execute(query: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    with transaction() as conn:
        cursor = conn.execute(query, params or ())
        return cursor.fetchone() if cursor.description else None


def ping() -> bool:
    try:
        row = fetch_one("select 1 as ok")
        return bool(row and row["ok"] == 1)
    except Exception:
        return False
