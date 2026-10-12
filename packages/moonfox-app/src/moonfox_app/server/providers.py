"""Administrator-only provider/model routes; business rules stay in the catalog service."""

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from ._http import dto_response, principal, request_model
from ._ports import ServerHub
from .schema import (
    ConfigView,
    Deleted,
    ManagedModelView,
    ModelCatalog,
    ProviderCatalog,
    ProviderTestResult,
    ProviderView,
    PutModel,
    PutProvider,
    RevisionCommand,
    SetDefaultModel,
)


def register_provider_routes(app: FastAPI, hub: ServerHub) -> None:
    @app.get("/api/v1/providers")
    async def providers(request: Request) -> JSONResponse:
        return dto_response(ProviderCatalog, await hub.providers(principal(request, admin=True)))

    @app.put("/api/v1/providers/{identity}")
    async def put_provider(request: Request, identity: str) -> JSONResponse:
        actor = principal(request, admin=True)
        command = await request_model(request, PutProvider)
        value = await hub.put_provider(
            actor,
            identity,
            settings=command.settings,
            expected_revision=command.expected_revision,
            secret_action=command.secret_action,
            api_key=command.api_key.get_secret_value() if command.api_key is not None else None,
        )
        return dto_response(ProviderView, value)

    @app.post("/api/v1/providers/{identity}/delete")
    async def delete_provider(request: Request, identity: str) -> JSONResponse:
        actor = principal(request, admin=True)
        command = await request_model(request, RevisionCommand)
        return dto_response(
            Deleted,
            await hub.delete_provider(actor, identity, expected_revision=command.expected_revision),
        )

    @app.post("/api/v1/providers/{identity}/test")
    async def test_provider(request: Request, identity: str) -> JSONResponse:
        actor = principal(request, admin=True)
        command = await request_model(request, RevisionCommand)
        return dto_response(
            ProviderTestResult,
            await hub.test_provider(actor, identity, expected_revision=command.expected_revision),
        )

    @app.get("/api/v1/models")
    async def models(request: Request) -> JSONResponse:
        return dto_response(ModelCatalog, await hub.managed_models(principal(request, admin=True)))

    @app.put("/api/v1/models/{identity}")
    async def put_model(request: Request, identity: str) -> JSONResponse:
        actor = principal(request, admin=True)
        command = await request_model(request, PutModel)
        return dto_response(
            ManagedModelView,
            await hub.put_model(
                actor,
                identity,
                settings=command.settings,
                expected_revision=command.expected_revision,
            ),
        )

    @app.post("/api/v1/models/{identity}/delete")
    async def delete_model(request: Request, identity: str) -> JSONResponse:
        actor = principal(request, admin=True)
        command = await request_model(request, RevisionCommand)
        return dto_response(
            Deleted,
            await hub.delete_model(actor, identity, expected_revision=command.expected_revision),
        )

    @app.post("/api/v1/models/{identity}/default")
    async def set_default(request: Request, identity: str) -> JSONResponse:
        actor = principal(request, admin=True)
        command = await request_model(request, SetDefaultModel)
        return dto_response(
            ConfigView,
            await hub.set_default_model(
                actor, identity, expected_revision=command.expected_revision
            ),
        )
