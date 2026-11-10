# Database connections

Vinculum names each database under `connections` in the main JSON config. A query selects the connection by that name through `database_connection`. The examples in this guide use the alias `reporting` and include a complete main config for each engine.

## Choose a configuration

| Backend | Config file | Connection details |
| --- | --- | --- |
| SQLite | [config.sqlite.json](../example/connections/config.sqlite.json) | Existing file at `example/demo.sqlite3`, created by the demo seed script |
| PostgreSQL | [config.postgres.json](../example/connections/config.postgres.json) | TCP host and port, database, login, and `VINCULUM_POSTGRES_PASSWORD` |
| MySQL | [config.mysql.json](../example/connections/config.mysql.json) | TCP host and port, database, login, and `VINCULUM_MYSQL_PASSWORD` |
| MariaDB | [config.mariadb.json](../example/connections/config.mariadb.json) | TCP host and port, database, login, and `VINCULUM_MARIADB_PASSWORD` |
| Oracle | [config.oracle.json](../example/connections/config.oracle.json) | Easy Connect DSN, login, and `VINCULUM_ORACLE_PASSWORD` |

These files use the configuration schema from Vinculum 0.1.1. JSON files accept unique keys and the fields supported by the selected `type`. The placeholder hosts, logins, and digest should be replaced for your deployment.

## Run a connection probe

1. From the repository root, install the extra for your backend, such as `python -m pip install '.[postgres]'`. MySQL and MariaDB both use `.[mysql]`. Oracle uses `.[oracle]`. SQLite is included with the base install.
2. Select one of the config files and set its connection values. For the SQLite example, first run `python example/seed_demo.py`. For a network database, use a dedicated read account and put the referenced password variable in `example/connections/.env` or in the service process environment. See [.env.example](../example/connections/.env.example).
3. Run `vinculum token`, replace `REPLACE_WITH_SHA256_DIGEST` in the selected config with its digest, and keep the raw bearer token for the request.

Start the selected example. This PostgreSQL command uses port 8001 so it can run alongside an instance on port 8000.

```bash
vinculum serve --config example/connections/config.postgres.json --port 8001
```

The configured probe runs a simple `SELECT` over the selected connection. Replace `YOUR_BEARER_TOKEN` with the token you generated.

```bash
curl -fsS http://127.0.0.1:8001/queries/database_probe \
  -H 'Authorization: Bearer YOUR_BEARER_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

```json
{"rows":[{"ok":1}],"row_count":1,"truncated":false}
```

The Oracle example selects from `dual` using its own [endpoints file](../example/connections/endpoints.oracle.json). The other configs use the [shared endpoints file](../example/connections/endpoints.json). A successful probe verifies authentication, connection setup, and a basic query. Check the privileges for your application tables or views with a query against those objects.

## Read-only accounts

Use the account examples with an administrator in the intended database. Replace the object names, host, and password placeholders with values for your deployment. Test login from the machine that runs Vinculum.

### SQLite

The example uses `"path": "../demo.sqlite3"`. Since the config lives in `example/connections/`, this resolves to `example/demo.sqlite3`. For a systemd deployment, a path such as `/var/lib/vinculum/cache.db` keeps the database accessible to the service user.

Vinculum opens the database with SQLite's read-only URI and enables `query_only`. The service user needs permission to traverse the parent directories and read the file. With WAL enabled and the sample systemd unit's read-only filesystem view, make the existing `-wal` and `-shm` files accessible. For distributing a point-in-time snapshot, use SQLite's backup mechanism. See [SQLite's WAL documentation](https://sqlite.org/wal.html#read_only_databases).

### PostgreSQL

Use `"type": "postgres"`. The sample connects to the `reporting` database on port 5432. Give the login `CONNECT` on the database, `USAGE` on the schema, and `SELECT` on the tables or views it needs. In a `psql` session connected to `reporting`, `\password` prompts for the password.

```sql
CREATE ROLE vinculum_reader LOGIN;
\password vinculum_reader
GRANT CONNECT ON DATABASE reporting TO vinculum_reader;
GRANT USAGE ON SCHEMA reporting_api TO vinculum_reader;
GRANT SELECT ON TABLE reporting_api.measurements TO vinculum_reader;
```

The strict audit includes inherited and assumable roles, ownership, database and schema creation privileges, and table, column, sequence, and routine access. A role with direct `SELECT` can still inherit `TEMPORARY` from `PUBLIC`. On older or upgraded databases, `PUBLIC` may also have `CREATE` on schema `public`. For a dedicated reporting database, review these revokes with the DBA. They change the default privileges available to other accounts in that database. See [PostgreSQL privileges](https://www.postgresql.org/docs/current/ddl-priv.html) and [schema defaults](https://www.postgresql.org/docs/current/ddl-schemas.html).

```sql
REVOKE TEMPORARY ON DATABASE reporting FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

For database TLS with hostname verification, libpq can read `PGSSLMODE=verify-full` and `PGSSLROOTCERT=/etc/vinculum/postgres-root.pem` from Vinculum's **process environment**, for example from systemd service settings. Use a database hostname that matches the certificate. Vinculum reads the JSON-referenced passwords from its dotenv file. Driver environment settings belong in the actual service environment. See [libpq environment variables](https://www.postgresql.org/docs/current/libpq-envars.html).

### MySQL and MariaDB

Choose `"type": "mysql"` for MySQL or `"type": "mariadb"` for MariaDB. Both examples use port 3306 and the `reporting` database. MySQL-style accounts include a client host. Replace the example address with the connection source as seen by the database server.

```sql
CREATE USER 'vinculum_reader'@'192.0.2.40'
  IDENTIFIED BY 'REPLACE_WITH_A_GENERATED_PASSWORD';
GRANT SELECT ON reporting.measurements TO 'vinculum_reader'@'192.0.2.40';
SHOW GRANTS FOR 'vinculum_reader'@'192.0.2.40';
```

The strict audit accepts supported direct `SELECT` and `USAGE` grant forms. Role assignments, grant options, and unrecognized grant forms fail the audit. MySQL's mandatory roles also fail the audit. For MariaDB, the audit examines `PUBLIC` grants as well. See [MySQL GRANT](https://dev.mysql.com/doc/refman/8.0/en/grant.html) and [MariaDB GRANT](https://mariadb.com/docs/server/reference/sql-statements/account-management-sql-statements/grant).

### Oracle

Use `"type": "oracle"` and a `dsn` such as `oracle.example.internal:1521/REPORTINGPDB`. The part after the slash is the database service name. Vinculum uses python-oracledb Thin mode and passes the DSN through to the driver. See the [python-oracledb connection guide](https://python-oracledb.readthedocs.io/en/latest/user_guide/connection_handling.html).

Create the account in the intended pluggable database and grant session access plus direct read access to the objects. Keep it separate from the object owner.

```sql
CREATE USER vinculum_reader IDENTIFIED BY "REPLACE_WITH_A_GENERATED_PASSWORD";
GRANT CREATE SESSION TO vinculum_reader;
GRANT READ ON reporting_owner.measurements TO vinculum_reader;
```

The strict audit checks session, system, object, column, and applicable schema privileges. It expects a dedicated account with direct read grants, no assigned roles, and no owned objects. See [Oracle GRANT](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/GRANT.html).

## Use several databases

Put the desired entries together under `connections` with distinct aliases, such as `live_reporting` and `snapshot`. In your endpoints file, choose the alias for each route. Relative SQL paths are resolved from that endpoints file.

```json
{
  "endpoints": {
    "live_probe": {
      "database_connection": "live_reporting",
      "query_file": "queries/probe.sql"
    },
    "snapshot_probe": {
      "database_connection": "snapshot",
      "query_file": "queries/probe.sql"
    }
  }
}
```

Give each referenced login its password variable and keep one alias per source when routes should share the same capacity limits. Startup connects to every configured source. Reverse-proxy HTTPS protects the HTTP client connection. Configure network security between Vinculum and the database according to the database's deployment requirements.

## Shared connection settings

| Field | Default | Allowed values | Purpose |
| --- | --- | --- | --- |
| `pool_size` | 2 | Integer 1 to 64 | Maximum sessions for this alias per Vinculum process, including idle sessions |
| `queue_size` | 16 | Integer 0 to 1,024 | Maximum waiting requests for this alias. Use zero for immediate rejection when the pool is busy. |
| `queue_timeout` | 5 | More than 0 and up to 3,600 seconds | Maximum wait to enter the pool |
| `connect_timeout` | 5 | Integer 1 to 300 seconds | Initial network connection or SQLite lock wait |

PostgreSQL defaults to port 5432. MySQL and MariaDB default to 3306. The Oracle port comes from the DSN. SQLite uses a file path.

## Query deadlines

The optional endpoint field `max_duration` accepts more than 0 and up to 3,600 seconds. The timer starts after a connection has been acquired and prepared. A query that reaches the deadline can return HTTP 504 with code `query_timeout`. When the field is omitted, existing database limits apply.

| Backend | Mechanism |
| --- | --- |
| SQLite | Virtual machine progress callback and reduced lock wait |
| PostgreSQL | Transaction-local `statement_timeout` with the remaining budget before a fetch |
| MySQL | Session `max_execution_time` for eligible read-only `SELECT` statements |
| MariaDB | Session `max_statement_time` |
| Oracle | Driver `call_timeout` for each database round trip |

Network stalls, database timer granularity, and cursor cleanup can extend elapsed HTTP time. For large results, use a SQL-level limit or paging filter, especially with MySQL and MariaDB where the streaming driver can drain unread rows. See [PostgreSQL timeouts](https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-STATEMENT-TIMEOUT), [MySQL timeouts](https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_max_execution_time), [MariaDB timeouts](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/query-optimizations/aborting-statements), and [python-oracledb connection attributes](https://python-oracledb.readthedocs.io/en/latest/api_manual/connection.html).
