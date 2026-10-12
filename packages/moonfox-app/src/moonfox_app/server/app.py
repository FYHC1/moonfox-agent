"""Optional HTTP/WS adapter. Explicit construction and lifespan are the only startup paths."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from pydantic import Field
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from moonfox_app.config import SessionSettings
from moonfox_app.security.identity import AccessDenied, AuthenticationError, TokenAuth
from moonfox_app.services.errors import BudgetExceeded, ConflictError, NotFoundError

from ._http import (
    HTTPBoundary,
    dto_response,
    error_response,
    exception_response,
    normalize_origin,
    principal,
    request_model,
    response_model,
)
from ._ports import ServerHub
from ._websocket import serve_events
from .export import protocol_document
from .schema import (
    DTO,
    PROTOCOL_VERSION,
    ApprovalCommand,
    ApprovalView,
    ConfigView,
    ControlCommand,
    CreateSession,
    Receipt,
    SessionSummary,
    Snapshot,
    SubmitInput,
    UpdateConfig,
    UpdateHostConfig,
)


class _SessionConfigView(DTO):
    revision: int = Field(ge=0)
    settings: SessionSettings


def create_server(
    hub: ServerHub,
    auth: TokenAuth,
    *,
    allowed_origins: Sequence[str],
    static_dir: Path | None = None,
    manage_lifecycle: bool = True,
) -> FastAPI:
    """Inject host-owned services; disconnecting a client only ends its subscription."""
    origins = frozenset(normalize_origin(origin) for origin in allowed_origins)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            try:
                await hub.start()
                yield
            finally:
                await hub.aclose()
        else:
            yield

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.add_middleware(HTTPBoundary, auth=auth, origins=tuple(origins))
    for error in (
        AuthenticationError,
        AccessDenied,
        NotFoundError,
        ConflictError,
        BudgetExceeded,
        RequestValidationError,
        HTTPException,
    ):
        app.add_exception_handler(error, exception_response)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "protocol_version": PROTOCOL_VERSION}

    @app.get("/api/v1/me")
    async def me(request: Request) -> dict[str, Any]:
        identity = principal(request)
        return {"id": identity.id, "is_admin": identity.is_admin}

    @app.get("/api/v1/sessions")
    async def sessions(request: Request) -> JSONResponse:
        values = await hub.list_sessions(principal(request))
        return JSONResponse(
            {
                "sessions": [
                    response_model(SessionSummary, row).model_dump(mode="json") for row in values
                ]
            }
        )

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(request: Request) -> JSONResponse:
        command = await request_model(request, CreateSession)
        value = await hub.create_session(
            principal(request), title=command.title, request_id=command.request_id
        )
        return dto_response(SessionSummary, value, status=201)

    @app.get("/api/v1/sessions/{session_id}")
    async def snapshot(request: Request, session_id: str) -> JSONResponse:
        return dto_response(Snapshot, await hub.snapshot(principal(request), session_id))

    @app.post("/api/v1/sessions/{session_id}/inputs", status_code=202)
    async def submit_input(request: Request, session_id: str) -> JSONResponse:
        command = await request_model(request, SubmitInput)
        value = await hub.submit_input(
            principal(request),
            session_id,
            text=command.text,
            request_id=command.request_id,
            idempotency_key=command.idempotency_key,
        )
        return dto_response(Receipt, value, status=202)

    @app.post("/api/v1/sessions/{session_id}/control", status_code=202)
    async def control(request: Request, session_id: str) -> JSONResponse:
        command = await request_model(request, ControlCommand)
        value = await hub.control(
            principal(request),
            session_id,
            action=command.action,
            request_id=command.request_id,
            input_id=command.input_id,
        )
        return dto_response(Receipt, value, status=202)

    @app.put("/api/v1/sessions/{session_id}/config")
    async def session_config(request: Request, session_id: str) -> JSONResponse:
        command = await request_model(request, UpdateConfig)
        value = await hub.update_session_config(
            principal(request),
            session_id,
            settings=command.settings,
            expected_revision=command.expected_revision,
        )
        return dto_response(_SessionConfigView, value)

    @app.get("/api/v1/config")
    async def config(request: Request) -> JSONResponse:
        return dto_response(ConfigView, await hub.config(principal(request, admin=True)))

    @app.put("/api/v1/config")
    async def update_config(request: Request) -> JSONResponse:
        identity = principal(request, admin=True)
        command = await request_model(request, UpdateHostConfig)
        value = await hub.update_config(
            identity, settings=command.settings, expected_revision=command.expected_revision
        )
        return dto_response(ConfigView, value)

    @app.post("/api/v1/sessions/{session_id}/approvals/{approval_id}")
    async def approve(request: Request, session_id: str, approval_id: str) -> JSONResponse:
        command = await request_model(request, ApprovalCommand)
        value = await hub.resolve_approval(
            principal(request), session_id, approval_id, decision=command.decision
        )
        return dto_response(ApprovalView, value)

    @app.get("/api/v1/protocol")
    async def protocol(request: Request) -> dict[str, Any]:
        principal(request)
        return protocol_document()

    @app.websocket("/api/v1/sessions/{session_id}/events")
    async def events(websocket: WebSocket, session_id: str) -> None:
        await serve_events(websocket, hub, auth, origins, session_id)

    # Never let the static mount answer unknown API paths (even if it contains an api directory).
    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"])
    async def missing_api(path: str) -> JSONResponse:
        return error_response(404)

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir.resolve(), html=True, follow_symlink=False))
    return app
