# Vinculum

Vinculum serves read-only database queries over an authenticated JSON API. You define each query in a SQL file, select its database in configuration, and give the endpoint a name. Applications can call endpoints such as `POST /queries/time_series` with named values. Vinculum validates the values, binds them through SQLAlchemy, and returns rows and result metadata.

A single instance can query SQLite, PostgreSQL, MySQL, MariaDB, and Oracle. Each named connection has its own pool and queue. Swagger UI and OpenAPI describe the configured routes and parameters.

## Quick start with SQLite

Use Python 3.11 or later. Run these commands from the repository root.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python example/seed_demo.py
vinculum token
```

Put the generated **SHA-256 digest** in `example/config.json` in place of `REPLACE_WITH_SHA256_DIGEST`. Keep the generated **bearer token** for the client.

```bash
vinculum serve --config example/config.json
```

Open [Swagger UI](http://127.0.0.1:8000/docs) and paste the bearer token into **Authorize**. To call the same route from a shell, replace `YOUR_BEARER_TOKEN` below.

```bash
curl -fsS http://127.0.0.1:8000/queries/time_series \
  -H 'Authorization: Bearer YOUR_BEARER_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"series_id":1,"start":"2026-01-01T00:00:00Z","end":"2026-01-02T00:00:00Z"}'
```

```json
{
  "rows": [
    {"timestamp": "2026-01-01T00:00:00Z", "value": 21.5},
    {"timestamp": "2026-01-01T01:00:00Z", "value": 21.8}
  ],
  "row_count": 2,
  "truncated": false
}
```

`GET /health` reports API liveness. `/docs`, `/redoc`, and `/openapi.json` are public. Query routes use bearer authentication.

## Configure a deployment

| File | Purpose |
| --- | --- |
| `config.json` | Named database connections, token digests, audit mode, and paths to the other configuration files |
| `endpoints.json` | Public endpoint names, database aliases, SQL file paths, parameter types, and per-query limits |
| `.env` | Password values for the variables named by `password_env_var` |
| `queries/*.sql` | Operator-maintained read queries |

The defaults are `.env` for `env_file` and `endpoints.json` for `endpoints_file`. Those paths and relative SQLite database paths are resolved from the directory of the main config. A relative `query_file` is resolved from the endpoints file's directory. A process environment variable takes precedence over its value in the password file. Restart Vinculum after editing configuration, tokens, grants, or SQL.

Generate separate tokens for each client and deployment. Store their digests under distinct labels in `tokens`. Every valid token for an instance can call every configured query on that instance. Remove a digest and restart to revoke the corresponding token. Keep password files and client tokens outside version control.

| Database | JSON `type` | Optional install extra | Example |
| --- | --- | --- | --- |
| SQLite | `sqlite` | Included | [config.sqlite.json](example/connections/config.sqlite.json) |
| PostgreSQL 14+ | `postgres` | `postgres` | [config.postgres.json](example/connections/config.postgres.json) |
| MySQL 8.0+ | `mysql` | `mysql` | [config.mysql.json](example/connections/config.mysql.json) |
| MariaDB 10.11+ | `mariadb` | `mysql` | [config.mariadb.json](example/connections/config.mariadb.json) |
| Oracle 19c+ | `oracle` | `oracle` | [config.oracle.json](example/connections/config.oracle.json) |

Install one driver with a command such as `python -m pip install '.[postgres]'` or all drivers with `python -m pip install '.[all]'`. The [database connection guide](docs/database-connections.md) walks through the examples, a read-only account for each server, and a query that verifies the connection.

Vinculum opens and checks every configured database during startup. Use one connection alias per database when routes should share a pool. For separate instances on the same host, give each instance its own config directory, service name, and `--port`.

### Publish a query

Add a named entry under `endpoints` in the endpoints file. It becomes a route under `/queries/`. This example uses the SQLite demo connection and its supplied SQL file.

```json
{
  "endpoints": {
    "time_series_many": {
      "description": "Get measurements for a list of series IDs.",
      "database_connection": "local_sqlite",
      "query_file": "queries/time_series_many.sql",
      "parameters": {
        "series_ids": "integer_list"
      },
      "max_duration": 2,
      "max_rows": 1000
    }
  }
}
```

The `database_connection` value names a key in the main config's `connections` object. Endpoint names begin with a letter and may contain letters, digits, underscores, and hyphens. The optional `description` is visible in OpenAPI.

Use one read-only `SELECT`, including a CTE or set operation if needed. Write `:name` for each client value. The JSON body uses those names as keys. Queries without parameters accept an empty body or `{}`. Undeclared parameters accept JSON strings, numbers, booleans, and null. Use the `parameters` map for `string`, `integer`, `number`, `boolean`, `decimal`, `date`, `datetime`, `integer_list`, or `string_list`. Send ISO 8601 strings for dates and datetimes. Write `CAST(:name AS type)` for casts and `\:` for a literal colon that looks like a bind in a string or comment.

For a list filter, use `IN :name` so SQLAlchemy can add the parentheses and bind each item. Lists support up to 100 values.

| SQL filter | Endpoint `parameters` | JSON body |
| --- | --- | --- |
| `series_id IN :series_ids` | `{"series_ids":"integer_list"}` | `{"series_ids":[1,2,3]}` |
| `status IN :statuses` | `{"statuses":"string_list"}` | `{"statuses":["active","pending"]}` |

Give result columns unique names with `AS` aliases where needed. `row_count` counts rows in the response. `truncated` indicates that more rows were available beyond `max_rows`, which defaults to 1,000 and can be set up to 10,000. For paging, use explicit filters and a deterministic `ORDER BY` in the SQL. Decimals are returned as strings, date and time values as ISO 8601 strings, binary values as base64url, and nonfinite floats as null.

### Capacity, deadlines, and grants

`pool_size` caps sessions per named connection per process. `queue_size` caps waiting requests, and `queue_timeout` bounds their wait in seconds. `connect_timeout` controls initial connection setup or SQLite's lock wait. The [connection settings reference](docs/database-connections.md#shared-connection-settings) lists the defaults and ranges.

`max_duration` applies to query execution and fetching after connection acquisition and read-only transaction setup. HTTP time also includes queueing, connection setup, and cleanup. Driver and database timeouts work differently. The [deadline reference](docs/database-connections.md#query-deadlines) explains those differences.

Use dedicated database read accounts and keep the SQL templates under deployment control. The SQL check rejects multiple statements, writes inside CTEs, `SELECT INTO`, and locking reads. It also rejects executable comments and optimizer hints for MySQL and MariaDB. `strict_mode` defaults to `true` and rejects disallowed or unverifiable grants at startup. With `strict_mode: false`, the audit logs warnings and queries run in read-only transactions or SQLite's read-only mode. The [connection guide](docs/database-connections.md#read-only-accounts) covers the grant checks by backend.

## Deploy with systemd

The included [service unit](deploy/vinculum.service) runs a single process as an unprivileged user and binds to `127.0.0.1:8000`. For a new installation on Debian or a similar Linux distribution, run from the repository root. Use the install extra for your database or `.[all]` for several drivers.

```bash
sudo useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin vinculum
sudo install -d -m 0755 /opt/vinculum
sudo python3 -m venv /opt/vinculum/.venv
sudo /opt/vinculum/.venv/bin/python -m pip install '.[postgres]'
sudo install -d -o root -g vinculum -m 0750 /etc/vinculum /etc/vinculum/queries
sudo install -m 0644 deploy/vinculum.service /etc/systemd/system/vinculum.service
```

Place the config, endpoint definitions, and query files under `/etc/vinculum` and give the service group read access. Make `.env` owned by `root:vinculum` with mode `0640`. For SQLite, use a readable path such as `/var/lib/vinculum/cache.db`. The unit restricts home directory access.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vinculum
sudo systemctl status vinculum
curl -fsS http://127.0.0.1:8000/health
```

For an upgrade, install from the updated repository and restart the service. Keep your deployment configuration in its separate directory.

```bash
sudo /opt/vinculum/.venv/bin/python -m pip install --upgrade .
sudo systemctl restart vinculum
sudo journalctl -u vinculum -n 50 --no-pager
```

## Serve through a reverse proxy

Terminate HTTPS at the proxy. For a subdomain, route the full site to Vinculum. For a mount such as `/vinculum`, strip that prefix before forwarding. `--root-path /vinculum` sets the public prefix for application redirects. Swagger UI and ReDoc resolve the schema relative to the docs page, so the same route must forward `/vinculum/openapi.json`.

For Caddy on a dedicated subdomain, use this site block.

```caddyfile
vinculum.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

For a path on an existing Caddy site, add the redirect and handler inside that site's block. See [Caddy's `handle_path` reference](https://caddyserver.com/docs/caddyfile/directives/handle_path).

```caddyfile
redir /vinculum /vinculum/docs 308
handle_path /vinculum/* {
    reverse_proxy 127.0.0.1:8000
}
```

For Nginx, put this inside the existing HTTPS server block. The trailing slash in `proxy_pass` replaces the matched location prefix. For a dedicated subdomain, use `location /` and `proxy_pass http://127.0.0.1:8000;`. See [Nginx's `proxy_pass` reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass).

```nginx
location = /vinculum {
    return 308 /vinculum/docs;
}
location /vinculum/ {
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_pass http://127.0.0.1:8000/;
}
```

### Windows RStudio clients

When using the Vinculum R client with a corporate proxy exception and a private HTTPS CA, configure the API hostname and the PEM CA file in the project's `.Renviron`. On a Windows R `curl` build with OpenSSL available, select OpenSSL at session startup for custom CA bundles. Restart the R session after editing the file. See the [R curl Windows guide](https://jeroen.r-universe.dev/curl/doc/windows.html).

```dotenv
CURL_SSL_BACKEND=openssl
VINCULUM_NOPROXY=api.example.internal
VINCULUM_CA_BUNDLE="C:/Users/your-name/certs/api-ca.pem"
```

## Troubleshoot a deployment

Start with local `/health`, then request the public URL through the proxy. After that, call a query or the [database probe](docs/database-connections.md#run-a-connection-probe) with a bearer token. These checks separate startup, routing, and query execution.

- **Service exits on startup.** Read `journalctl -u vinculum -n 50` and check driver extras, paths, password variables, database reachability, and the strict grant audit.
- **Swagger reports a schema 404.** Request `/vinculum/openapi.json` directly and check the proxy's prefix rule.
- **HTTP 403 through a corporate network.** Check the response headers and body for a proxy denial. Compare the same host, method, and path with the command-line request.

Use `error.code` for client logic and `error.message` for details. Requests have a 64 KiB body limit. Authenticated database failures can include the driver's message, including internal names or values.

| HTTP | Error code | Check |
| --- | --- | --- |
| 400 | `invalid_json` | JSON syntax and duplicate keys |
| 401 | `unauthorized` | Raw bearer token in the `Authorization` header and its digest in `tokens` |
| 413 | `body_too_large` | Body size against the 64 KiB limit |
| 415 | `unsupported_media_type` | `Content-Type: application/json` for a request body |
| 422 | `invalid_parameters` | JSON keys and types against OpenAPI. List types expect arrays, including for a single value. |
| 500 | `invalid_result` | Unique SQL column aliases and supported result values |
| 502 | `query_failed` | Driver message, SQL template, and read grants |
| 503 | `database_busy` | Pool and queue saturation for the source used by the route |
| 503 | `database_unavailable` | Database connectivity and login |
| 504 | `query_timeout` | SQL execution and the endpoint's `max_duration` |

## Run the tests

```bash
python -m pip install '.[test]'
python -m pytest -q
```

The included tests use SQLite on disk without external services. See the [database connection guide](docs/database-connections.md) for an authenticated smoke query on a deployed database.
