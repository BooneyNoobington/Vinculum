import asyncio
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from time import monotonic

import anyio
from pydantic_core import to_json
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import QueuePool

from .config import ConfigError, ConnectionConfig, OracleSettings, SQLiteSettings
from .privileges import privilege_problems
from .queries import Query

log = logging.getLogger(__name__)

READ_ONLY = {
    "postgres": "SET TRANSACTION READ ONLY",
    "oracle": "SET TRANSACTION READ ONLY",
    "mysql": "START TRANSACTION READ ONLY",
    "mariadb": "START TRANSACTION READ ONLY",
}
MIN_VERSIONS = {"postgres": (14,), "mysql": (8, 0), "mariadb": (10, 11), "oracle": (19,)}
STREAMING = {"postgres", "mysql", "mariadb"}
FETCH_SIZE = 128
NATIVE_TIMEOUT_CODES = {
    "57014",  # PostgreSQL query canceled
    3024, 1969,  # MySQL, MariaDB
    "DPY-4024", "DPI-1067", "DPI-1080", "ORA-01013",
    sqlite3.SQLITE_INTERRUPT,
}


class QueryFailure(Exception):
    """A public error code and optional diagnostic for an authenticated query."""


def make_engine(settings: ConnectionConfig, password: str | None) -> Engine:
    kind = settings.type
    options = {}
    if isinstance(settings, SQLiteSettings):
        url = URL.create(
            "sqlite+pysqlite", database=settings.path.as_uri(), query={"mode": "ro", "uri": "true"}
        )
        options = {"check_same_thread": False, "timeout": settings.connect_timeout}
    elif isinstance(settings, OracleSettings):
        url = URL.create("oracle+oracledb")
        options = {
            "dsn": settings.dsn,
            "user": settings.user,
            "password": password,
            "tcp_connect_timeout": settings.connect_timeout,
        }
    else:
        driver = "postgresql+psycopg" if kind == "postgres" else f"{kind}+pymysql"
        url = URL.create(
            driver,
            username=settings.user,
            password=password,
            host=settings.host,
            port=settings.port,
            database=settings.database,
        )
        options = {"connect_timeout": settings.connect_timeout}
        if kind in {"mysql", "mariadb"}:
            options["charset"] = "utf8mb4"

    engine = create_engine(
        url,
        connect_args=options,
        poolclass=QueuePool,
        pool_size=settings.pool_size,
        max_overflow=0,
        pool_timeout=settings.queue_timeout,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    if kind == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(raw, _record):
            raw.execute("PRAGMA query_only = ON")
            raw.execute("PRAGMA trusted_schema = OFF")

    return engine


def error_code(error: DBAPIError) -> int | str | None:
    original = error.orig
    first = original.args[0] if original.args else None
    code = (
        getattr(original, "sqlite_errorcode", None)
        or getattr(first, "full_code", None)
        or (first if type(first) is int else None)
        or getattr(original, "sqlstate", None)
    )
    if type(code) is int or isinstance(code, str) and re.fullmatch(r"[A-Z0-9-]{1,16}", code):
        return code
    return None


@contextmanager
def query_budget(conn: Connection, kind: str, seconds: float | None, busy_seconds: int):
    """Yield a callback that arms or checks the remaining budget before each fetch."""
    if seconds is None:
        yield lambda: None
        return

    deadline = monotonic() + seconds

    def remaining():
        left = deadline - monotonic()
        if left <= 0:
            raise QueryFailure("query_timeout")
        return left

    def remaining_ms():
        return max(1, math.ceil(remaining() * 1000))

    if kind == "postgres":
        def arm():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {remaining_ms()}")

        yield arm
        remaining()
    elif kind == "sqlite":
        raw = conn.connection.driver_connection
        raw.execute(f"PRAGMA busy_timeout = {min(remaining_ms(), busy_seconds * 1000)}")
        raw.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
        try:
            yield remaining
            remaining()
        except DBAPIError as exc:
            if error_code(exc) == sqlite3.SQLITE_BUSY and monotonic() >= deadline:
                raise QueryFailure("query_timeout") from None
            raise
        finally:
            raw.set_progress_handler(None, 0)
            raw.execute(f"PRAGMA busy_timeout = {busy_seconds * 1000}")
    elif kind == "oracle":
        raw = conn.connection.driver_connection
        previous = raw.call_timeout

        def arm():
            timeout = remaining_ms()
            raw.call_timeout = min(previous, timeout) if previous else timeout

        try:
            yield arm
            remaining()
        finally:
            if not conn.invalidated:
                raw.call_timeout = previous
    else:
        variable = "max_execution_time" if kind == "mysql" else "max_statement_time"
        current = conn.exec_driver_sql(f"SELECT @@SESSION.{variable}").scalar_one()
        timeout = remaining_ms() if kind == "mysql" else remaining()
        if current:
            timeout = min(timeout, current)
        conn.exec_driver_sql(f"SET SESSION {variable} = {timeout}")
        try:
            yield remaining
            remaining()
        finally:
            if not conn.invalidated:
                try:
                    conn.exec_driver_sql(f"SET SESSION {variable} = {current}")
                except SQLAlchemyError:
                    conn.invalidate()
                    log.warning("Discarded a database session after a timeout setting could not be reset.")


class Database:
    def __init__(self, settings: ConnectionConfig, engine: Engine):
        self.settings = settings
        self.engine = engine
        self.slots = asyncio.Semaphore(settings.pool_size)
        self.worker_limit = anyio.CapacityLimiter(settings.pool_size)
        self.in_flight = 0

    def check(self, name: str, strict: bool) -> None:
        kind = self.settings.type
        try:
            conn = self.engine.connect()
        except DBAPIError as exc:
            raise ConfigError(f"Cannot connect to {name!r}. {exc.orig}") from None
        except SQLAlchemyError:
            raise ConfigError(
                f"Cannot connect to {name!r}. Check its address, credentials and server status."
            ) from None

        with conn:
            minimum = MIN_VERSIONS.get(kind)
            if minimum and conn.dialect.server_version_info < minimum:
                raise ConfigError(f"Connection {name!r} is using an unsupported server version.")
            if kind == "mysql" and conn.dialect.is_mariadb:
                raise ConfigError(f"Connection {name!r} needs type 'mariadb'.")

            try:
                problems = privilege_problems(conn, kind)
            except SQLAlchemyError:
                problems = ["privileges could not be verified"]

        if problems:
            message = f"Read-only privilege check for {name!r}: {', '.join(problems)}."
            if strict:
                raise ConfigError(message)
            log.warning("%s", message)

    async def run(self, query: Query, parameters: dict) -> bytes:
        if self.in_flight >= self.settings.pool_size + self.settings.queue_size:
            raise QueryFailure("database_busy")

        self.in_flight += 1
        try:
            try:
                async with asyncio.timeout(self.settings.queue_timeout):
                    await self.slots.acquire()
            except TimeoutError:
                raise QueryFailure("database_busy") from None

            try:
                return await anyio.to_thread.run_sync(
                    self._execute, query, parameters, limiter=self.worker_limit
                )
            finally:
                self.slots.release()
        finally:
            self.in_flight -= 1

    def _execute(self, query: Query, parameters: dict) -> bytes:
        kind = self.settings.type
        try:
            conn = self.engine.connect()
        except PoolTimeout:
            raise QueryFailure("database_busy") from None
        except DBAPIError as exc:
            log.warning("Connection for endpoint %s failed (%s).", query.name, error_code(exc))
            raise QueryFailure("database_unavailable", str(exc.orig)) from None

        try:
            with conn:
                if kind in READ_ONLY:
                    conn.exec_driver_sql(READ_ONLY[kind])
                with query_budget(
                    conn, kind, query.settings.max_duration, self.settings.connect_timeout
                ) as arm:
                    options = {"yield_per": FETCH_SIZE} if kind in STREAMING else {}
                    statement = query.statement_for(parameters)
                    arm()
                    with conn.execute(statement, parameters, execution_options=options) as result:
                        columns = list(result.keys())
                        if len(columns) != len(set(columns)):
                            log.error("Endpoint %s returned duplicate column names.", query.name)
                            raise QueryFailure("invalid_result", "Use unique aliases for duplicate column names.")

                        limit = query.settings.max_rows
                        rows = []
                        while len(rows) <= limit:
                            arm()
                            batch = result.fetchmany(min(FETCH_SIZE, limit + 1 - len(rows)))
                            if not batch:
                                break
                            rows.extend(dict(zip(columns, row, strict=True)) for row in batch)
        except DBAPIError as exc:
            code = error_code(exc)
            if code in NATIVE_TIMEOUT_CODES:
                raise QueryFailure("query_timeout", str(exc.orig)) from None
            log.warning("Query for endpoint %s failed (%s).", query.name, code)
            raise QueryFailure("query_failed", str(exc.orig)) from None

        truncated = len(rows) > limit
        if truncated:
            rows.pop()
        return to_json(
            {"rows": rows, "row_count": len(rows), "truncated": truncated},
            bytes_mode="base64",
            inf_nan_mode="null",
        )
