import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

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
API_PREFIX = "/v1"
REQUEST_ID_HEADER = "X-Request-ID"
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
    "internal_error": (500, "An unexpected error occurred while handling the request."),
}


class APIError(Exception):
    pass


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


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
        openapi_url=f"{API_PREFIX}/openapi.json",
        # The schema is one level below the deployment root. Its paths include /v1.
        servers=[{"url": ".."}],
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
    async def error_response(request: Request, exc: APIError | QueryFailure):
        code, *details = exc.args
        status, message = ERRORS[code]
        if details:
            message = f"{message} {details[0]}"
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(
            {"error": {"code": code, "message": message, "request_id": request.state.request_id}},
            status_code=status,
            headers=headers,
        )

    @app.middleware("http")
    async def identify_request(request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception as exc:
            log.error("request_id=%s unexpected_error=%s", request_id, type(exc).__name__)
            response = await error_response(request, APIError("internal_error"))
        response.headers[REQUEST_ID_HEADER] = request_id
        route = request.scope.get("route")
        log.info(
            "request_id=%s method=%s route=%s status=%s",
            request_id,
            request.method,
            getattr(route, "path", "-"),
            response.status_code,
        )
        return response

    @app.get(f"{API_PREFIX}/docs", include_in_schema=False)
    async def swagger():
        return get_swagger_ui_html(openapi_url="openapi.json", title="Vinculum - Swagger UI")

    @app.get(f"{API_PREFIX}/redoc", include_in_schema=False)
    async def redoc():
        return get_redoc_html(openapi_url="openapi.json", title="Vinculum - ReDoc")

    request_id_headers = {
        REQUEST_ID_HEADER: {
            "description": "Server-generated ID for this request.",
            "schema": {"type": "string"},
        }
    }

    @app.get("/health", tags=["Health"], responses={200: {"headers": request_id_headers}})
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
                log.error(
                    "request_id=%s endpoint=%s invalid_result=%s",
                    request.state.request_id,
                    query.name,
                    type(exc).__name__,
                )
                raise APIError("invalid_result") from None
            return Response(payload, media_type="application/json")

        return execute

    responses = {
        status: {"model": ErrorResponse, "description": description, "headers": request_id_headers}
        for status, description in ERRORS.values()
    }
    responses[200] = {"headers": request_id_headers}
    responses[500]["description"] = "The query service could not produce a result."
    for query in queries:
        app.add_api_route(
            f"{API_PREFIX}/queries/{query.name}",
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
