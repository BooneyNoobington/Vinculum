"""Conservative startup checks for dedicated read accounts."""
import re

from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError


POSTGRES = """
WITH roles AS (
    SELECT *
    FROM pg_catalog.pg_roles
    WHERE rolname = current_user OR pg_catalog.pg_has_role(current_user, oid, 'MEMBER')
)
SELECT 'administrative or predefined role' WHERE EXISTS (
    SELECT 1 FROM roles
    WHERE rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        OR (left(rolname, 3) = 'pg_' AND rolname <> 'pg_read_all_data')
)
UNION ALL
SELECT 'database ownership, creation or temporary table rights' WHERE EXISTS (
    SELECT 1 FROM roles r
    CROSS JOIN pg_catalog.pg_database d
    WHERE d.datname = current_database()
        AND (d.datdba = r.oid
             OR pg_catalog.has_database_privilege(r.oid, d.oid, 'CREATE,TEMPORARY'))
)
UNION ALL
SELECT 'schema ownership or creation rights' WHERE EXISTS (
    SELECT 1 FROM roles r
    CROSS JOIN pg_catalog.pg_namespace n
    WHERE n.nspowner = r.oid OR pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE')
)
UNION ALL
SELECT 'relation ownership, write or grant rights' WHERE EXISTS (
    SELECT 1 FROM roles r
    CROSS JOIN pg_catalog.pg_class c
    WHERE c.relowner = r.oid OR CASE
        WHEN c.relkind IN ('r', 'p', 'v', 'm', 'f') THEN
            pg_catalog.has_table_privilege(r.oid, c.oid, '{table_privileges}')
            OR pg_catalog.has_any_column_privilege(
                r.oid, c.oid, 'INSERT,REFERENCES,SELECT WITH GRANT OPTION'
            )
            -- PUBLIC can update pg_settings to change its own session settings.
            OR (c.oid <> 'pg_catalog.pg_settings'::pg_catalog.regclass
                AND pg_catalog.has_any_column_privilege(r.oid, c.oid, 'UPDATE'))
        WHEN c.relkind = 'S' THEN
            pg_catalog.has_sequence_privilege(r.oid, c.oid, 'UPDATE,USAGE,SELECT WITH GRANT OPTION')
        ELSE false
    END
)
UNION ALL
SELECT 'routine ownership or callable custom security-definer code' WHERE EXISTS (
    SELECT 1 FROM roles r
    CROSS JOIN pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON p.pronamespace = n.oid
    WHERE p.proowner = r.oid OR (
        p.prosecdef AND n.nspname <> 'pg_catalog'
        AND pg_catalog.has_schema_privilege(r.oid, n.oid, 'USAGE')
        AND pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE')
    )
)
"""

ORACLE = """
SELECT 'administrative session, system privileges or assigned roles' FROM dual
WHERE SYS_CONTEXT('USERENV', 'ISDBA') = 'TRUE'
    OR EXISTS (SELECT 1 FROM sys.session_privs WHERE privilege <> 'CREATE SESSION')
    OR EXISTS (SELECT 1 FROM sys.user_sys_privs WHERE admin_option = 'YES')
    OR EXISTS (SELECT 1 FROM sys.user_role_privs)
    OR EXISTS (SELECT 1 FROM sys.session_roles)
UNION ALL
SELECT 'owned objects' FROM dual
WHERE EXISTS (SELECT 1 FROM sys.user_objects)
    OR EXISTS (SELECT 1 FROM sys.user_db_links)
UNION ALL
SELECT 'object write, executable or grant privileges' FROM dual
WHERE EXISTS (
    SELECT 1 FROM sys.all_tab_privs p
    WHERE p.grantee IN (USER, 'PUBLIC')
        AND (p.privilege NOT IN ('SELECT', 'READ') OR p.grantable = 'YES'
             OR p.type = 'SEQUENCE')
        AND NOT (
            p.grantee = 'PUBLIC' AND p.grantable = 'NO' AND (
                (p.privilege = 'INHERIT PRIVILEGES' AND p.type = 'USER')
                OR (p.privilege = 'EXECUTE' AND EXISTS (
                    SELECT 1 FROM sys.all_users u
                    WHERE u.username = p.table_schema AND u.oracle_maintained = 'Y'
                ))
            )
        )
)
UNION ALL
SELECT 'column write or grant privileges' FROM dual
WHERE EXISTS (
    SELECT 1 FROM sys.all_col_privs
    WHERE grantee IN (USER, 'PUBLIC')
        AND (privilege NOT IN ('SELECT', 'READ') OR grantable = 'YES')
)
"""

# SHOW GRANTS emits SQL, including role grants and SELECT plus column-level grants.
# Match only known read grants. Quoted identifiers may contain commas or keywords.
_BACKTICK = r"`(?:[^`]|``)*`"
_SINGLE_QUOTE = r"'(?:[^'\\]|''|\\.)*'"
_IDENTIFIER = rf"(?:{_BACKTICK}|[A-Za-z0-9_$]+)"
_OBJECT = rf"(?:\*|{_IDENTIFIER})"
_ACCOUNT = rf"(?:{_BACKTICK}|{_SINGLE_QUOTE})@(?:{_BACKTICK}|{_SINGLE_QUOTE})"
_PLUGIN = rf"{_IDENTIFIER}(?: (?:USING|AS) {_SINGLE_QUOTE})?"
_AUTH = rf"(?: IDENTIFIED (?:BY PASSWORD {_SINGLE_QUOTE}|VIA {_PLUGIN}(?: OR {_PLUGIN})*))?"
MYSQL_READ_GRANT = re.compile(
    rf"GRANT (?:USAGE|SELECT(?: \({_IDENTIFIER}(?:,\s*{_IDENTIFIER})*\))?)"
    rf" ON {_OBJECT}\.{_OBJECT} TO (?:{_ACCOUNT}|PUBLIC){_AUTH}",
    re.IGNORECASE,
)


def mysql_privileges(conn: Connection, kind: str) -> list[str]:
    if kind == "mysql":
        mandatory_roles = conn.exec_driver_sql("SELECT @@GLOBAL.mandatory_roles").scalar_one()
        if mandatory_roles:
            return ["server-wide mandatory roles"]

    grants = list(conn.exec_driver_sql("SHOW GRANTS FOR CURRENT_USER").scalars())
    if not grants:
        return ["no account grants could be inspected"]

    if kind == "mariadb":
        try:
            grants.extend(conn.exec_driver_sql("SHOW GRANTS FOR PUBLIC").scalars())
        except DBAPIError as exc:
            if not exc.orig.args or exc.orig.args[0] != 1141:
                raise

    if any(not MYSQL_READ_GRANT.fullmatch(grant) for grant in grants):
        return ["grants other than direct SELECT or USAGE, or an unrecognized grant form"]
    return []


def privilege_problems(conn: Connection, kind: str) -> list[str]:
    version = conn.dialect.server_version_info
    if kind in {"mysql", "mariadb"}:
        return mysql_privileges(conn, kind)
    if kind == "postgres":
        table_privileges = "INSERT,DELETE,TRUNCATE,REFERENCES,TRIGGER,SELECT WITH GRANT OPTION"
        if version >= (17,):
            table_privileges += ",MAINTAIN"
        return list(conn.exec_driver_sql(POSTGRES.format(table_privileges=table_privileges)).scalars())
    if kind == "oracle":
        problems = list(conn.exec_driver_sql(ORACLE).scalars())
        if version >= (23,):
            schema_privileges = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM sys.session_schema_privs"
            ).scalar_one()
            if schema_privileges:
                problems.append("schema privileges")
        return problems
    # SQLite engines open the main file in mode=ro and enable PRAGMA query_only.
    if conn.exec_driver_sql("PRAGMA query_only").scalar_one() != 1:
        return ["SQLite query_only is disabled"]
    return []
