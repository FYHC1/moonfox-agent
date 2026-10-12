"""Generic managed-extension boundary, independent of optional business distributions."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError

from moonfox_agent.api import AgentTool, AgentToolResult
from moonfox_ai.api import (
    FauxProvider,
    FauxResponse,
    Model,
    TextContent,
    get_current_tools,
)
from moonfox_app.api import (
    ConflictError,
    ExtensionConfiguration,
    HostSettings,
    ModelRegistration,
    Principal,
    SessionSettings,
    TokenAuth,
    create_application,
)
from moonfox_app.extension_api import (
    ExtensionConflictError,
    ExtensionManifest,
    ExtensionRecordNotFoundError,
    SessionExtensionDefinition,
)
from moonfox_app.security import AccessDenied
from moonfox_app.server.app import create_server
from moonfox_app.services.errors import BudgetExceeded, InvalidConfiguration, NotFoundError

ADMIN = Principal("owner", True)
MODEL = Model("offline", "faux", "faux", max_tokens=2048)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    note: str = "probe"


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Probe:
    def __init__(self, resources, raw):
        self.resources = resources
        self.settings = Settings.model_validate_json(raw)
        self.ticket = None
        self.detached = False

    async def setup(self, api):
        api.on("agent_settled", self.settled)
        api.register_tool(AgentTool("probe.status", "status", "status", Empty, execute=self.tool))

    async def attach(self, ctx):
        assert ctx.binding == self.resources.binding

    async def settled(self, event):
        self.ticket = self.resources.models.current_ticket()

    async def tool(self, *args):
        return AgentToolResult([TextContent("ok")])

    async def detach(self):
        self.detached = True

    async def dispose(self):
        self.detached = True


def make(tmp_path, *, settings=None, responses=None, definitions=None, created=None):
    instances = []
    management = []
    faux = FauxProvider(responses if responses is not None else [FauxResponse.text("ok")] * 8)

    def factory(resources, raw):
        plugin = Probe(resources, raw)
        instances.append(plugin)
        if created is not None:
            created.set()
        return plugin

    async def manage(resources, command, raw):
        assert resources.models is None
        values = json.loads(raw)
        if values.get("fail") == "conflict":
            raise ExtensionConflictError
        if values.get("fail") == "missing":
            raise ExtensionRecordNotFoundError
        if values.get("fail") == "invalid":
            raise ValueError("private input must not be echoed")
        management.append(resources)
        return json.dumps({"session": resources.binding.session_id, "domain": resources.domain})

    definition = SessionExtensionDefinition(
        ExtensionManifest("probe", "0.1.0", ">=0.1,<0.2", "session", ("events", "tools", "models")),
        factory,
        lambda raw: Settings.model_validate_json(raw).model_dump_json(),
        json.dumps(Settings.model_json_schema()),
        Settings().model_dump_json(),
        tool_names=("probe.status",),
        commands=("list",),
        manage=manage,
    )
    hub = create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(MODEL, faux.stream, "offline")},
        defaults=HostSettings(defaults=settings or SessionSettings(enabled_extensions=("probe",))),
        session_extensions=definitions if definitions is not None else [definition],
    )
    return hub, faux, instances, management, definition


async def new_session(hub, name="one"):
    return (await hub.create_session(ADMIN, title=name, request_id=name))["id"]


async def prompt(hub, identity, request_id="input"):
    receipt = await hub.submit_input(
        ADMIN, identity, text="hello", request_id=request_id, idempotency_key=request_id
    )
    await hub.wait_for_idle(identity)
    return receipt


def test_extension_config_is_immutable_finite_and_backward_compatible():
    assert SessionSettings.model_validate_json('{"model_id":"demo"}').extension_config == ()
    config = ExtensionConfiguration(extension_id="probe", config_json='{"note":"hello"}')
    with pytest.raises(ValidationError):
        config.config_json = "{}"
    for raw in ("[]", "null", '{"value":NaN}', '{"value":Infinity}', "bad"):
        with pytest.raises(ValidationError):
            ExtensionConfiguration(extension_id="probe", config_json=raw)
    with pytest.raises(ValidationError):
        SessionSettings(extension_config=(config, config))


async def test_management_is_authorized_and_does_not_start_runtime_or_model(tmp_path):
    hub, faux, instances, management, _ = make(tmp_path, settings=SessionSettings())
    await hub.start()
    try:
        identity = await new_session(hub)
        before = await hub.snapshot(ADMIN, identity)
        result = await hub.extension_command(
            ADMIN,
            identity,
            "probe",
            command="list",
            arguments_json="{}",
            expected_revision=1,
            request_id="manage",
        )
        assert json.loads(result["result_json"]) == {"session": identity, "domain": "agent"}
        assert not instances and not faux.requests
        assert (await hub.snapshot(ADMIN, identity))["messages"] == before["messages"]
        assert management[0].data_dir.is_relative_to(tmp_path / "extensions" / "probe" / "agent")
        with pytest.raises(AccessDenied):
            await hub.extension_command(
                Principal("owner"),
                identity,
                "probe",
                command="list",
                arguments_json="{}",
                expected_revision=1,
                request_id="denied",
            )
        with pytest.raises(ConflictError):
            await hub.extension_command(
                ADMIN,
                identity,
                "probe",
                command="list",
                arguments_json="{}",
                expected_revision=0,
                request_id="stale",
            )
        with pytest.raises(NotFoundError):
            await hub.extension_command(
                ADMIN,
                identity,
                "probe",
                command="shell",
                arguments_json="{}",
                expected_revision=1,
                request_id="unknown",
            )
        catalog = await hub.config(ADMIN)
        assert catalog["extensions"][0]["config_schema"]["additionalProperties"] is False
        assert catalog["tools"] == ["probe.status"]
    finally:
        await hub.aclose()


async def test_configuration_validation_grants_and_idle_unload(tmp_path):
    hub, faux, instances, _, _ = make(tmp_path)
    await hub.start()
    try:
        identity = await new_session(hub)
        await prompt(hub, identity)
        assert not get_current_tools(faux.requests[0][1].messages)
        old = instances[0]
        settings = SessionSettings(
            allowed_tools=("probe.status",),
            enabled_extensions=("probe",),
            extension_config=(
                ExtensionConfiguration(extension_id="probe", config_json='{"note":"changed"}'),
            ),
        )
        with pytest.raises(AccessDenied):
            await hub.update_session_config(
                Principal("owner"), identity, settings=settings, expected_revision=1
            )
        await hub.update_session_config(ADMIN, identity, settings=settings, expected_revision=1)
        await hub.wait_for_idle(identity)
        assert old.detached
        with pytest.raises(ConflictError):
            await old.resources.models.complete(
                old.ticket, system="system", text="text", max_tokens=64
            )
        await prompt(hub, identity, "second")
        assert instances[-1].settings.note == "changed"
        assert [tool.name for tool in get_current_tools(faux.requests[-1][1].messages)] == [
            "probe.status"
        ]
        with pytest.raises(ConflictError):
            await hub.update_session_config(ADMIN, identity, settings=settings, expected_revision=1)
        bad = settings.model_copy(
            update={
                "extension_config": (
                    ExtensionConfiguration(extension_id="probe", config_json='{"domain":"qq"}'),
                )
            }
        )
        with pytest.raises(InvalidConfiguration):
            await hub.update_session_config(ADMIN, identity, settings=bad, expected_revision=2)
        await hub.update_session_config(
            ADMIN, identity, settings=SessionSettings(), expected_revision=2
        )
        await hub.wait_for_idle(identity)
        assert instances[-1].detached
        await prompt(hub, identity, "disabled")
        assert not get_current_tools(faux.requests[-1][1].messages)
    finally:
        await hub.aclose()


async def test_model_tickets_share_original_budget_and_reject_other_session(tmp_path):
    settings = SessionSettings(
        max_turns_per_run=1, max_requests_per_input=2, enabled_extensions=("probe",)
    )
    hub, faux, instances, _, _ = make(tmp_path, settings=settings)
    await hub.start()
    try:
        first, second = await new_session(hub), await new_session(hub, "two")
        await prompt(hub, first)
        await prompt(hub, second)
        left, right = instances
        port = left.resources.models
        assert port.current_ticket() is None
        with pytest.raises(ConflictError):
            await port.complete(right.ticket, system="system", text="text", max_tokens=64)
        assert len(faux.requests) == 2
        assert (
            await port.complete(
                left.ticket, system="extract only", text="one new input", max_tokens=64
            )
            == "ok"
        )
        assert len(faux.requests) == 3
        context = faux.requests[-1][1].messages
        assert not get_current_tools(context)
        assert len([m for m in context if m.role == "user"]) == 1
        assert faux.requests[-1][2].max_tokens == 64
        with pytest.raises(BudgetExceeded):
            await port.complete(left.ticket, system="again", text="text", max_tokens=64)
        assert len(faux.requests) == 3
        snapshot = await hub.snapshot(ADMIN, first)
        assert snapshot["inputs"][0]["requests_used"] == 2
        assert len(snapshot["messages"]) == 2  # background result is not a chat message
    finally:
        await hub.aclose()
    with pytest.raises(ConflictError):
        await port.complete(left.ticket, system="after close", text="text", max_tokens=64)


async def test_hot_config_does_not_cancel_foreground_but_retires_at_settle(tmp_path):
    created = asyncio.Event()
    hub, _, instances, _, _ = make(
        tmp_path,
        responses=[FauxResponse.text("response", delay_ms=40, chunk_size=1)],
        created=created,
    )
    await hub.start()
    try:
        identity = await new_session(hub)
        await hub.submit_input(
            ADMIN, identity, text="hello", request_id="one", idempotency_key="one"
        )
        async with asyncio.timeout(5):
            await created.wait()
        await hub.update_session_config(
            ADMIN, identity, settings=SessionSettings(), expected_revision=1
        )
        await hub.wait_for_idle(identity)
        assert (await hub.snapshot(ADMIN, identity))["inputs"][0]["status"] == "completed"
        assert instances[0].detached
    finally:
        await hub.aclose()


@pytest.mark.parametrize(
    "failure,expected", [("conflict", 409), ("missing", 404), ("invalid", 422)]
)
def test_http_management_contract_and_non_echoing_errors(tmp_path, failure, expected):
    hub, _, _, _, _ = make(tmp_path)
    auth = TokenAuth({"a" * 32: ADMIN, "b" * 32: Principal("visitor")})
    with TestClient(create_server(hub, auth, allowed_origins=["http://testserver"])) as client:
        headers = {"Authorization": "Bearer " + "a" * 32}
        identity = client.post(
            "/api/v1/sessions", headers=headers, json={"title": "test", "request_id": "create"}
        ).json()["id"]
        path = f"/api/v1/sessions/{identity}/extensions/probe/commands/list"
        body = {"request_id": "cmd", "expected_revision": 1, "arguments_json": "{}"}
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200
        assert json.loads(response.json()["result_json"])["domain"] == "agent"
        assert client.post(path, json=body).status_code == 401
        assert (
            client.post(
                path, headers={"Authorization": "Bearer " + "b" * 32}, json=body
            ).status_code
            == 403
        )
        response = client.post(
            path, headers=headers, json={**body, "arguments_json": json.dumps({"fail": failure})}
        )
        assert response.status_code == expected
        assert "private" not in response.text
        assert client.post(path, headers=headers, json={**body, "domain": "qq"}).status_code == 422


def test_failed_registration_releases_host_lease(tmp_path):
    hub, _, _, _, definition = make(tmp_path)
    asyncio.run(hub.aclose())
    with pytest.raises(ValueError, match="unique"):
        make(tmp_path, definitions=[definition, definition])
    clean, *_ = make(tmp_path)
    asyncio.run(clean.aclose())


def test_unavailable_configuration_rejected_and_no_business_defaults_added(tmp_path):
    with pytest.raises(InvalidConfiguration):
        create_application(
            home=tmp_path,
            models={},
            defaults=HostSettings(
                defaults=SessionSettings(
                    extension_config=(ExtensionConfiguration(extension_id="unknown"),)
                )
            ),
        )
    hub = create_application(home=tmp_path / "clean", models={})
    assert not hub._extensions
    asyncio.run(hub.aclose())


async def test_configuration_changed_during_detach_uses_latest_revision(tmp_path, monkeypatch):
    hub, _, instances, _, _ = make(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    original = Probe.detach

    async def slow_detach(plugin):
        if plugin is instances[0]:
            started.set()
            await release.wait()
        await original(plugin)

    def configured(note):
        return SessionSettings(
            enabled_extensions=("probe",),
            extension_config=(
                ExtensionConfiguration(
                    extension_id="probe", config_json=json.dumps({"note": note})
                ),
            ),
        )

    await hub.start()
    try:
        identity = await new_session(hub)
        await prompt(hub, identity)
        monkeypatch.setattr(Probe, "detach", slow_detach)
        await hub.update_session_config(
            ADMIN, identity, settings=configured("middle"), expected_revision=1
        )
        async with asyncio.timeout(5):
            await started.wait()
        await hub.update_session_config(
            ADMIN, identity, settings=configured("latest"), expected_revision=2
        )
        await hub.submit_input(ADMIN, identity, text="new", request_id="new", idempotency_key="new")
        release.set()
        await hub.wait_for_idle(identity)
        assert instances[-1].settings.note == "latest"
    finally:
        release.set()
        await hub.aclose()
