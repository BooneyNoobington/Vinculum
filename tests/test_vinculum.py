import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from threading import Event
from urllib.parse import urljoin

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from vinculum.app import create_app
from vinculum.config import ConfigError, EndpointSettings, SQLiteSettings
from vinculum.database import Database, QueryFailure, error_code, make_engine
from vinculum.privileges import MYSQL_READ_GRANT
from vinculum.queries import compile_query

TOKEN = "fixture-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def config_file(tmp_path):
    database = tmp_path / "private # source.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE private_records (id INTEGER, value TEXT);
            INSERT INTO private_records VALUES (1, 'first'), (2, 'second');
        """)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "tokens": {"fixture": hashlib.sha256(TOKEN.encode()).hexdigest()},
        "connections": {
            "private_source": {"type": "sqlite", "path": database.name, "pool_size": 1}
        },
    }))
    return config


def configure_endpoints(config_file, endpoints):
    configured = {}
    for name, (sql, settings) in endpoints.items():
        sql_file = config_file.with_name(f"{name}.sql")
        sql_file.write_text(sql)
        configured[name] = {
            "database_connection": "private_source",
            "query_file": sql_file.name,
            **settings,
        }
    config_file.with_name("endpoints.json").write_text(json.dumps({"endpoints": configured}))


@asynccontextmanager
async def api_client(config_file, root_path=""):
    app = create_app(config_file)
    transport = httpx.ASGITransport(app=app, root_path=root_path)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


def test_bearer_body_and_public_openapi(config_file):
    configure_endpoints(config_file, {
        "lookup": (
            "SELECT value AS label FROM private_records WHERE id IN :ids ORDER BY id;",
            {"parameters": {"ids": "integer_list"}, "max_rows": 1},
        )
    })

    async def scenario():
        async with api_client(config_file, root_path="/vinculum") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            schema = await client.get("/openapi.json")
            assert "private_" not in schema.text
            operation = schema.json()["paths"]["/queries/lookup"]["post"]
            assert operation["security"] == [{"BearerAuth": []}]
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            assert body["properties"]["ids"]["items"] == {"type": "integer"}

            response = await client.post(f"/queries/lookup?token={TOKEN}", json={"ids": [1]})
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"] == "Bearer"

            response = await client.post("/queries/lookup", headers=AUTH, json={"ids": ["1 OR 1=1"]})
            assert response.status_code == 422
            assert "ids[0]" in response.json()["error"]["message"]
            assert "valid integer" in response.json()["error"]["message"]
            assert "OR 1" not in response.text

            response = await client.post("/queries/lookup", headers=AUTH, json={"id": [1]})
            assert response.status_code == 422
            assert "Missing required parameters 'ids'" in response.text
            assert "Unexpected parameters 'id'" in response.text

            response = await client.post("/queries/lookup", headers=AUTH, json={"ids": [1, 2]})
            assert response.json() == {
                "rows": [{"label": "first"}], "row_count": 1, "truncated": True
            }
            response = await client.post("/queries/lookup", headers=AUTH, json={"ids": []})
            assert response.json() == {"rows": [], "row_count": 0, "truncated": False}

    asyncio.run(scenario())


@pytest.mark.parametrize(("prefix", "root_path"), [
    ("", ""), ("/vinculum", ""), ("/vinculum", "/vinculum"),
])
def test_docs_urls_resolve_under_the_public_prefix(config_file, prefix, root_path):
    configure_endpoints(config_file, {"lookup": ("SELECT 1 AS value", {})})

    async def scenario():
        async with api_client(config_file, root_path=root_path) as client:
            public_base = f"https://proxy.test{prefix}/"
            for page, pattern in [("docs", r"url: '([^']+)'"), ("redoc", r'spec-url="([^"]+)"')]:
                response = await client.get(f"/{page}")
                assert response.status_code == 200
                schema_url = re.search(pattern, response.text)[1]
                assert urljoin(public_base + page, schema_url) == public_base + "openapi.json"

            schema = (await client.get("/openapi.json")).json()
            server = urljoin(public_base + "openapi.json", schema["servers"][0]["url"])
            assert server == public_base

    asyncio.run(scenario())


def test_database_failure_includes_the_driver_message(config_file, caplog):
    configure_endpoints(config_file, {
        "broken": ("SELECT :value AS label FROM private_missing_table", {})
    })

    async def scenario():
        async with api_client(config_file) as client:
            response = await client.post("/queries/broken", json={"value": "secret"})
            assert response.status_code == 401
            assert "private_missing_table" not in response.text

            response = await client.post("/queries/broken", headers=AUTH, json={"value": "secret"})
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "query_failed"
            assert "no such table: private_missing_table" in response.json()["error"]["message"]
            assert "SELECT" not in response.text
            assert "secret" not in response.text
            assert "private_missing_table" not in caplog.text

    asyncio.run(scenario())


def test_invalid_json_reports_location_or_duplicate_key(config_file):
    configure_endpoints(config_file, {"lookup": ("SELECT :id AS value", {})})

    async def scenario():
        async with api_client(config_file) as client:
            headers = {**AUTH, "Content-Type": "application/json"}
            for body, detail in [(' {"id": }', "line 1, column"),
                                 ('{"id": 1, "id": 2}', "Duplicate JSON key 'id'")]:
                response = await client.post("/queries/lookup", headers=headers, content=body)
                assert response.status_code == 400
                assert response.json()["error"]["code"] == "invalid_json"
                assert detail in response.json()["error"]["message"]

    asyncio.run(scenario())


def test_sqlite_timeout_releases_and_resets_the_connection(config_file):
    configure_endpoints(config_file, {
        "slow": (
            "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x<100000000) "
            "SELECT sum(x) AS total FROM cnt",
            {"max_duration": 0.01},
        ),
        "quick": ("SELECT 1 AS ready", {}),
    })

    async def scenario():
        async with api_client(config_file) as client:
            response = await client.post("/queries/slow", headers=AUTH)
            assert response.status_code == 504
            assert response.json()["error"]["code"] == "query_timeout"
            response = await client.post("/queries/quick", headers=AUTH)
            assert response.json()["rows"] == [{"ready": 1}]

    asyncio.run(scenario())


def test_sqlite_engine_opens_the_file_read_only(config_file):
    settings = SQLiteSettings(type="sqlite", path=config_file.with_name("private # source.sqlite3"))
    engine = make_engine(settings, None)
    try:
        with engine.begin() as conn, pytest.raises(DBAPIError):
            conn.exec_driver_sql("INSERT INTO private_records VALUES (3, 'write')")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM private_records").scalar_one() == 2
    finally:
        engine.dispose()


def test_queue_is_bounded_per_database(config_file, monkeypatch):
    settings = SQLiteSettings(
        type="sqlite", path=config_file.with_name("private # source.sqlite3"),
        pool_size=1, queue_size=1, queue_timeout=0.15,
    )
    started = Event()
    release = Event()

    def blocking_query(_query, _params):
        started.set()
        assert release.wait(3)
        return b"done"

    async def scenario():
        engine = make_engine(settings, None)
        busy = Database(settings, engine)
        other = Database(settings, engine)
        monkeypatch.setattr(busy, "_execute", blocking_query)
        monkeypatch.setattr(other, "_execute", lambda *_: b"other")
        first = asyncio.create_task(busy.run(None, {}))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            second = asyncio.create_task(busy.run(None, {}))
            await asyncio.sleep(0)
            with pytest.raises(QueryFailure, match="database_busy"):
                await busy.run(None, {})
            assert await other.run(None, {}) == b"other"
            with pytest.raises(QueryFailure, match="database_busy"):
                await second
        finally:
            release.set()
            await first
            engine.dispose()
        assert busy.in_flight == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(("kind", "sql"), [
    ("sqlite", "SELECT 1; DELETE FROM t"),
    ("sqlite", "SELECT ?"),
    ("sqlite", "SELECT ':x' AS value"),
    ("postgres", "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"),
    ("postgres", "SELECT * INTO another FROM t"),
    ("postgres", "SELECT :x::int AS value"),
    ("mysql", "SELECT 1 /*!50000 INTO OUTFILE 'example' */"),
    ("mysql", "SELECT @x := 1"),
    ("mariadb", "SELECT id FROM t FOR UPDATE"),
    ("oracle", "SELECT s.NEXTVAL FROM dual"),
])
def test_write_syntax_and_ambiguous_binds_are_refused(tmp_path, kind, sql):
    path = tmp_path / "query.sql"
    path.write_text(sql)
    settings = EndpointSettings(database_connection="db", query_file=path)
    with pytest.raises(ConfigError):
        compile_query("query", settings, kind)


@pytest.mark.parametrize(("kind", "sql"), [
    ("postgres", "WITH t AS (SELECT CAST(:id AS integer) AS id) SELECT id FROM t"),
    ("sqlite", "SELECT 'drop table' AS label, :id AS id; -- explanation"),
    ("oracle", "SELECT :id AS id FROM dual;"),
    ("mysql", "SELECT :id AS id FROM DUAL"),
    ("mariadb", "SELECT :id AS id"),
])
def test_select_templates_compile_for_all_dialects(tmp_path, kind, sql):
    path = tmp_path / "query.sql"
    path.write_text(sql)
    settings = EndpointSettings(database_connection="db", query_file=path)
    query = compile_query("query", settings, kind)
    assert query.validate({"id": 42}) == {"id": 42}


@pytest.mark.parametrize(("grant", "accepted"), [
    ("GRANT USAGE ON *.* TO `user`@`localhost` IDENTIFIED BY PASSWORD '*HASH'", True),
    ("GRANT SELECT (`one`, `with,comma`) ON `db`.`table` TO `user`@`%`", True),
    ("GRANT SELECT ON `db`.* TO PUBLIC", True),
    ("GRANT SELECT (`one`), UPDATE (`two`) ON `db`.`table` TO `user`@`%`", False),
    ("GRANT SELECT ON `db`.* TO `user`@`%` WITH GRANT OPTION", False),
    ("GRANT `role`@`%` TO `user`@`%`", False),
])
def test_mysql_grant_allowlist(grant, accepted):
    assert bool(MYSQL_READ_GRANT.fullmatch(grant)) is accepted


def test_mariadb_timeout_uses_the_native_code():
    original = Exception(1969, "Statement timed out")
    original.sqlstate = "70100"
    assert error_code(DBAPIError(None, None, original)) == 1969
