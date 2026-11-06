import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

import sqlglot
from pydantic import AllowInfNan, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.sql.elements import TextClause
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from .config import ConfigError, EndpointSettings

FiniteNumber = Annotated[StrictFloat, AllowInfNan(False)]
Scalar = StrictStr | StrictInt | FiniteNumber | StrictBool | None
PARAMETER_TYPES = {
    "string": StrictStr,
    "integer": StrictInt,
    "number": FiniteNumber,
    "boolean": StrictBool,
    "decimal": Decimal,
    "date": date,
    "datetime": datetime,
    "integer_list": Annotated[list[StrictInt], Field(max_length=100)],
    "string_list": Annotated[list[StrictStr], Field(max_length=100)],
}
SQL_DIALECT = {"postgres": "postgres", "mariadb": "mysql", "mysql": "mysql",
               "oracle": "oracle", "sqlite": "sqlite"}
BIND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
BIND_IN_TEXT = re.compile(r"(?<![:\w\\]):\w+(?![:\w])")
BLOCKED_NODES = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock, exp.PropertyEQ)
BLOCKED_CALLS = {"load_extension", "load_file", "nextval", "setval", "writefile"}


@dataclass
class Query:
    name: str
    settings: EndpointSettings
    statement: TextClause
    parameters: dict[str, TypeAdapter]

    def validate(self, body: object) -> dict:
        if not isinstance(body, dict):
            raise ValueError("The body must be a JSON object.")

        missing = self.parameters.keys() - body.keys()
        extra = body.keys() - self.parameters.keys()
        problems = []
        if missing:
            problems.append(f"Missing required parameters {', '.join(map(repr, sorted(missing)))}.")
        if extra:
            problems.append(f"Unexpected parameters {', '.join(map(repr, sorted(extra)))}.")
        if problems:
            raise ValueError(" ".join(problems))

        values = {}
        for name, adapter in self.parameters.items():
            try:
                values[name] = adapter.validate_python(body[name])
            except ValidationError as exc:
                if name not in self.settings.parameters:
                    raise ValueError(
                        f"Parameter {name!r} must be a JSON string, number, boolean, or null."
                    ) from None
                error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
                path = name + "".join(f"[{index}]" for index in error["loc"])
                raise ValueError(f"Invalid parameter {path!r}. {error['msg']}.") from None
        return values

    def statement_for(self, parameters: dict) -> TextClause:
        # PostgreSQL defaults untyped empty sets to INTEGER. Leave nonempty values to driver inference.
        empty_strings = [
            bindparam(name, type_=String, expanding=True)
            for name, value in parameters.items()
            if value == [] and self.settings.parameters.get(name) == "string_list"
        ]
        return self.statement.bindparams(*empty_strings) if empty_strings else self.statement

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {name: adapter.json_schema() for name, adapter in self.parameters.items()},
            "required": list(self.parameters),
        }


def compile_query(name: str, settings: EndpointSettings, kind: str) -> Query:
    try:
        sql = settings.query_file.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise ConfigError(f"Cannot read the SQL template for endpoint {name!r}.") from None

    dialect = SQL_DIALECT[kind]
    # MySQL and MariaDB execute some comments. Optimizer hints can also change timeouts.
    if dialect == "mysql" and re.search(r"/\*(?:!|[Mm]!|\+)", sql):
        raise ConfigError(f"Endpoint {name!r} uses an executable comment or optimizer hint.")

    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
        if tokens and tokens[-1].token_type is TokenType.SEMICOLON:
            separator = tokens.pop()
            sql = sql[:separator.start] + sql[separator.end + 1:]
        if any(token.token_type is TokenType.SEMICOLON for token in tokens):
            raise ConfigError(f"Endpoint {name!r} must contain a single SELECT.")
        # SQLGlot can log the original SQL when falling back to an unsupported command.
        parsed = sqlglot.parse(sql, read=dialect, error_message_context=0)
    except SqlglotError:
        raise ConfigError(f"Cannot parse the SQL template for endpoint {name!r}.") from None

    if len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.SetOperation)):
        raise ConfigError(f"Endpoint {name!r} must contain a single SELECT.")

    for node in parsed[0].walk():
        if (
            isinstance(node, BLOCKED_NODES)
            or isinstance(node, exp.Anonymous) and node.name.lower() in BLOCKED_CALLS
            or kind == "oracle" and isinstance(node, exp.Column) and node.name.upper() == "NEXTVAL"
        ):
            raise ConfigError(f"Endpoint {name!r} contains a potential write or locking operation.")

    binds = [node.name for node in parsed[0].find_all(exp.Placeholder)]
    if any(not BIND_NAME.fullmatch(bind) for bind in binds):
        raise ConfigError(f"Endpoint {name!r} must use named :parameter binds.")

    statement = text(sql)
    compiled = statement.compile(dialect=sqlite.dialect())
    bind_positions = {token.start for token in tokens if token.token_type is TokenType.COLON}
    if (
        any(match.start() not in bind_positions for match in BIND_IN_TEXT.finditer(sql))
        or Counter(compiled.positiontup or []) != Counter(binds)
    ):
        raise ConfigError(
            f"Endpoint {name!r} has ambiguous binds. Use CAST(:name AS type) for casts "
            r"and \: for literal colons that look like binds."
        )

    bind_names = set(binds)
    if settings.parameters.keys() - bind_names:
        raise ConfigError(f"Endpoint {name!r} declares a parameter absent from the SQL template.")

    expanding = [
        bindparam(bind, expanding=True)
        for bind, kind in settings.parameters.items()
        if kind.endswith("_list")
    ]
    parameters = {
        bind: TypeAdapter(PARAMETER_TYPES.get(settings.parameters.get(bind), Scalar))
        for bind in sorted(bind_names)
    }
    return Query(name, settings, statement.bindparams(*expanding), parameters)
