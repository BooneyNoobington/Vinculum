import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Request, Security
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from . import __version__
from .config import ConfigError, load_config, unique_keys
from .database import Database, QueryFailure, make_engine
from .queries import Query, compile_query

log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024
ERRORS = {
    "unauthorized": (401, "A valid bearer token is required."),
    "invalid_json": (400, "Send a valid JSON object with unique keys."),
    "invalid_parameters": (422, "Check the query parameters."),
    "body_too_large": (413, "The request body exceeds 64 KiB."),
    "unsupported_media_type": (415, "Send the request with Content-Type: application/json."),
    "database_busy": (503, "The query queue is full or the wait expired."),
    "database_unavailable": (503, "The query service is temporarily unavailable."),
    "query_timeout": (504, "The query exceeded its time limit."),
    "query_failed": (502, "The configured query could not be completed."),
    "invalid_result": (500, "The configured query could not produce a JSON result."),
}


class APIError(Exception):
    pass


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class QueryResponse(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


async def read_body(request: Request) -> object:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise APIError("body_too_large")
            body.extend(chunk)
    except ClientDisconnect:
        raise APIError("invalid_json", "The request ended before the body was read.") from None

    if not body:
        return {}
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise APIError("unsupported_media_type")

    def reject_constant(_constant):
        raise ValueError("JSON numbers must be finite.")

    try:
        return json.loads(body, object_pairs_hook=unique_keys, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        detail = f"{exc.msg} at line {exc.lineno}, column {exc.colno}."
        raise APIError("invalid_json", detail) from None
    except UnicodeDecodeError:
        raise APIError("invalid_json", "The request body has invalid text encoding.") from None
    except ValueError as exc:
        raise APIError("invalid_json", str(exc)) from None
    except RecursionError:
        raise APIError("invalid_json", "The request body is nested too deeply.") from None


def create_app(config_file: Path) -> FastAPI:
    config = load_config(config_file)
    queries = [
        compile_query(name, settings, config.service.connections[settings.database_connection].type)
        for name, settings in config.endpoints.items()
    ]
    databases: dict[str, Database] = {}

    @asynccontextmanager
    async def lifespan(_app):
        try:
            for name, settings in config.service.connections.items():
                try:
                    engine = make_engine(settings, config.passwords.get(name))
                except ImportError:
                    extra = "mysql" if settings.type == "mariadb" else settings.type
                    raise ConfigError(
                        f"Missing driver for {name!r}. Install vinculum-query-api[{extra}]."
                    ) from None
                databases[name] = Database(settings, engine)
                await anyio.to_thread.run_sync(
                    databases[name].check, name, config.service.strict_mode
                )
            yield
        finally:
            for database in databases.values():
                await anyio.to_thread.run_sync(database.engine.dispose)
            databases.clear()

    app = FastAPI(
        title="Vinculum",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        servers=[{"url": "."}],
        root_path_in_servers=False,
    )
    bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
    digests = [bytes.fromhex(digest) for digest in config.service.tokens.values()]

    async def require_token(
        credential: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> None:
        if credential is None or len(credential.credentials) > 512:
            raise APIError("unauthorized")
        digest = hashlib.sha256(credential.credentials.encode("utf-8")).digest()
        accepted = False
        for expected in digests:
            accepted |= hmac.compare_digest(digest, expected)
        if not accepted:
            raise APIError("unauthorized")

    @app.exception_handler(APIError)
    @app.exception_handler(QueryFailure)
    async def error_response(_request: Request, exc: APIError | QueryFailure):
        code, *details = exc.args
        status, message = ERRORS[code]
        if details:
            message = f"{message} {details[0]}"
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(
            {"error": {"code": code, "message": message}}, status_code=status, headers=headers
        )

    @app.get("/docs", include_in_schema=False)
    async def swagger():
        return get_swagger_ui_html(openapi_url="openapi.json", title="Vinculum - Swagger UI")

    @app.get("/redoc", include_in_schema=False)
    async def redoc():
        return get_redoc_html(openapi_url="openapi.json", title="Vinculum - ReDoc")

    @app.get("/health", tags=["Health"])
    async def health():
        return {"status": "ok"}

    def handler(query: Query):
        async def execute(request: Request) -> Response:
            body = await read_body(request)
            try:
                parameters = query.validate(body)
            except ValueError as exc:
                raise APIError("invalid_parameters", str(exc)) from None

            try:
                payload = await databases[query.settings.database_connection].run(query, parameters)
            except QueryFailure:
                raise
            except Exception as exc:
                log.error("Endpoint %s failed (%s).", query.name, type(exc).__name__)
                raise APIError("invalid_result") from None
            return Response(payload, media_type="application/json")

        return execute

    responses = {
        status: {"model": ErrorResponse, "description": description}
        for status, description in ERRORS.values()
    }
    for query in queries:
        app.add_api_route(
            f"/queries/{query.name}",
            handler(query),
            methods=["POST"],
            name=f"query_{query.name}",
            summary=query.name,
            description=query.settings.description or "",
            tags=["Queries"],
            dependencies=[Depends(require_token)],
            response_model=QueryResponse,
            responses=responses,
            openapi_extra={
                "requestBody": {
                    "required": bool(query.parameters),
                    "content": {"application/json": {"schema": query.json_schema()}},
                }
            },
        )
    return app
