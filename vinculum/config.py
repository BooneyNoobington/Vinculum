import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError


class ConfigError(Exception):
    """A safe-to-log startup configuration error."""


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
BindName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{64}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Seconds = Annotated[float, Field(gt=0, le=3600, allow_inf_nan=False)]
Port = Annotated[int, Field(ge=1, le=65535)]
ParameterType = Literal[
    "string", "integer", "number", "boolean", "decimal", "date", "datetime",
    "integer_list", "string_list",
]


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionSettings(SettingsModel):
    pool_size: int = Field(default=2, ge=1, le=64, strict=True)
    queue_size: int = Field(default=16, ge=0, le=1024, strict=True)
    queue_timeout: Seconds = 5
    connect_timeout: int = Field(default=5, ge=1, le=300, strict=True)


class SQLiteSettings(ConnectionSettings):
    type: Literal["sqlite"]
    path: Path


class PasswordSettings(ConnectionSettings):
    user: NonEmpty
    password_env_var: EnvName


class NetworkSettings(PasswordSettings):
    host: NonEmpty
    database: NonEmpty


class PostgresSettings(NetworkSettings):
    type: Literal["postgres"]
    port: Port = 5432


class MySQLSettings(NetworkSettings):
    type: Literal["mysql", "mariadb"]
    port: Port = 3306


class OracleSettings(PasswordSettings):
    type: Literal["oracle"]
    dsn: NonEmpty


ConnectionConfig = Annotated[
    SQLiteSettings | PostgresSettings | MySQLSettings | OracleSettings,
    Field(discriminator="type"),
]


class ServiceSettings(SettingsModel):
    strict_mode: bool = Field(default=True, strict=True)
    env_file: Path = Path(".env")
    endpoints_file: Path = Path("endpoints.json")
    tokens: dict[Identifier, Digest] = Field(min_length=1)
    connections: dict[Identifier, ConnectionConfig] = Field(min_length=1)


class EndpointSettings(SettingsModel):
    database_connection: Identifier
    query_file: Path
    description: str | None = None
    max_duration: Seconds | None = None
    max_rows: int = Field(default=1000, ge=1, le=10000, strict=True)
    parameters: dict[BindName, ParameterType] = Field(default_factory=dict)


class EndpointFile(SettingsModel):
    endpoints: dict[Identifier, EndpointSettings] = Field(min_length=1)


@dataclass
class Config:
    service: ServiceSettings
    endpoints: dict[str, EndpointSettings]
    passwords: dict[str, str]


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def read_settings(model, path):
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=unique_keys)
    except (OSError, ValueError):
        raise ConfigError(f"Cannot read {path} as JSON with unique keys.") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        fields = ", ".join(
            f"{'.'.join(map(str, error['loc'])) or '(root)'} ({error['type']})"
            for error in errors[:8]
        )
        raise ConfigError(f"Invalid settings in {path}. Check {fields}.") from None


def load_config(config_file: Path) -> Config:
    config_file = config_file.resolve()
    service = read_settings(ServiceSettings, config_file)
    env_file = config_file.parent / service.env_file
    endpoints_file = (config_file.parent / service.endpoints_file).resolve()
    endpoint_config = read_settings(EndpointFile, endpoints_file)

    try:
        env_values = dotenv_values(env_file, interpolate=False) if env_file.exists() else {}
    except (OSError, UnicodeError):
        raise ConfigError(f"Cannot read environment file {env_file}.") from None

    passwords = {}
    for name, connection in service.connections.items():
        if isinstance(connection, SQLiteSettings):
            connection.path = (config_file.parent / connection.path).resolve()
            if not connection.path.is_file():
                raise ConfigError(f"SQLite file for connection {name!r} is missing.")
        else:
            key = connection.password_env_var
            value = os.environ.get(key, env_values.get(key))
            if not value:
                raise ConfigError(f"Missing environment variable {key} for connection {name!r}.")
            passwords[name] = value

    for name, endpoint in endpoint_config.endpoints.items():
        if endpoint.database_connection not in service.connections:
            raise ConfigError(f"Endpoint {name!r} names an unknown database connection.")
        endpoint.query_file = (endpoints_file.parent / endpoint.query_file).resolve()

    return Config(service, endpoint_config.endpoints, passwords)
