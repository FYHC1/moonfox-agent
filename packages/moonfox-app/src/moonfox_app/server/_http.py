"""Bounded ASGI ingress and non-echoing HTTP validation for the optional adapter."""

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from moonfox_app.security.identity import AccessDenied, AuthenticationError, Principal, TokenAuth
from moonfox_app.services.errors import BudgetExceeded, ConflictError, NotFoundError

from .schema import ErrorResponse

MAX_BODY_BYTES = 128 * 1024
_METHODS = {"GET", "HEAD", "POST", "PUT", "OPTIONS"}
_HEADERS = {"authorization", "content-type"}
_ERRORS = {
    401: ("unauthorized", "Authentication required"),
    403: ("forbidden", "Access denied"),
    404: ("not_found", "Resource not found"),
    405: ("method_not_allowed", "Method not allowed"),
    409: ("conflict", "Request conflicts with current state"),
    413: ("request_too_large", "Request body too large"),
    422: ("invalid_request", "Invalid request"),
    429: ("budget_exceeded", "Request budget exceeded"),
    500: ("internal_error", "Internal server error"),
}


def error_response(status: int) -> JSONResponse:
    error, detail = _ERRORS.get(status, ("request_failed", "Request failed"))
    return JSONResponse(
        ErrorResponse(error=error, detail=detail).model_dump(mode="json"),
        status_code=status,
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


async def exception_response(request: Request, exc: Exception) -> JSONResponse:
    """Never expose exception messages, validation inputs, locations or context."""
    status = 500
    if isinstance(exc, AuthenticationError):
        status = 401
    elif isinstance(exc, AccessDenied):
        status = 403
    elif isinstance(exc, NotFoundError):
        status = 404
    elif isinstance(exc, ConflictError):
        status = 409
    elif isinstance(exc, BudgetExceeded):
        status = 429
    elif isinstance(exc, RequestValidationError):
        status = 422
    elif isinstance(exc, HTTPException):
        status = exc.status_code
    return error_response(status)


def normalize_origin(value: str) -> str:
    """Canonicalize a single HTTP origin, never an URL prefix or wildcard."""
    if (
        not value
        or len(value) > 2048
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in value for character in "*\\%@?#")
    ):
        raise ValueError("An explicit HTTP(S) origin is required")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        raise ValueError("An explicit HTTP(S) origin is required") from None
    if ":" in host:
        host = f"[{host}]"
    suffix = "" if port is None or port == (443 if parsed.scheme == "https" else 80) else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def allowed_origin(headers: Headers, origins: frozenset[str], *, required: bool) -> str | None:
    values = headers.getlist("origin")
    if not values and not required:
        return None
    if len(values) == 1:
        try:
            if normalize_origin(values[0]) in origins:
                return values[0]
        except ValueError:
            pass
    raise AccessDenied("Origin denied")


def principal(request: Request, *, admin: bool = False) -> Principal:
    identity = request.state.principal
    if not isinstance(identity, Principal):
        raise AuthenticationError
    if admin and not identity.is_admin:
        raise AccessDenied
    return identity


async def request_model[T: BaseModel](request: Request, model: type[T]) -> T:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(422)
    try:
        # JSON validation preserves strict scalar types but permits JSON arrays for tuple settings.
        return model.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(422) from None


def response_model[T: BaseModel](model: type[T], value: Any) -> T:
    # The hub exposes JSON-shaped dictionaries, not core state or Pydantic instances.
    return model.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))


def dto_response(model: type[BaseModel], value: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(response_model(model, value).model_dump(mode="json"), status_code=status)


class HTTPBoundary:
    """Authenticate before reading API bodies; bound even chunked requests before dispatch."""

    def __init__(self, app: ASGIApp, *, auth: TokenAuth, origins: Sequence[str]) -> None:
        self.app = app
        self.auth = auth
        self.origins = frozenset(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin: str | None = None
        started = False

        async def safe_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                if scope["path"].startswith("/api/"):
                    response_headers["Cache-Control"] = "no-store"
                if origin is not None:
                    response_headers["Access-Control-Allow-Origin"] = origin
                    response_headers.add_vary_header("Origin")
            await send(message)

        try:
            origin = allowed_origin(headers, self.origins, required=False)
            if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
                requested_headers = {
                    item.strip().lower()
                    for item in headers.get("access-control-request-headers", "").split(",")
                    if item.strip()
                }
                if (
                    origin is None
                    or headers["access-control-request-method"] not in _METHODS
                    or not requested_headers <= _HEADERS
                ):
                    raise AccessDenied
                response = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": ", ".join(sorted(_METHODS)),
                        "Access-Control-Allow-Headers": "Authorization, Content-Type",
                        "Access-Control-Max-Age": "600",
                    },
                )
                await response(scope, receive, safe_send)
                return
            if scope["path"] == "/api" or scope["path"].startswith("/api/"):
                credentials = headers.getlist("authorization")
                if len(credentials) != 1:
                    raise AuthenticationError
                scheme, separator, token = credentials[0].partition(" ")
                if (
                    scheme.lower() != "bearer"
                    or not separator
                    or not token
                    or any(character.isspace() for character in token)
                ):
                    raise AuthenticationError
                identity = self.auth.authenticate(token)
                scope.setdefault("state", {})["principal"] = identity
                # No API route uses query parameters. In particular credentials never belong here.
                if scope.get("query_string"):
                    raise HTTPException(422)
            lengths = headers.getlist("content-length")
            if len(lengths) > 1 or (lengths and not lengths[0].isascii()):
                raise HTTPException(422)
            if lengths:
                if not lengths[0].isdigit():
                    raise HTTPException(422)
                if len(lengths[0]) > 10 or int(lengths[0]) > MAX_BODY_BYTES:
                    raise HTTPException(413)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    raise HTTPException(413)
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def bounded_receive() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, bounded_receive, safe_send)
        except Exception as exc:
            if started:
                raise
            response = await exception_response(Request(scope), exc)
            await response(scope, receive, safe_send)
