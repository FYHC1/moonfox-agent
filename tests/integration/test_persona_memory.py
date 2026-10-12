"""Application/HTTP integration of both optional extensions; no real QQ or paid APIs."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from moonfox_ai.api import (
    FauxProvider,
    FauxResponse,
    Model,
    get_current_system_prompt,
    get_current_tools,
)
from moonfox_app.api import (
    ChannelBinding,
    ConflictError,
    ExtensionConfiguration,
    HostSettings,
    ModelRegistration,
    NotFoundError,
    Principal,
    SessionSettings,
    TokenAuth,
    create_application,
)
from moonfox_app.server.app import create_server
from moonfox_ext_memory import EXTENSION as MEMORY
from moonfox_ext_persona import EXTENSION as PERSONA

ADMIN = Principal("test-owner", True)


def settings(*, extraction="off", requests=4):
    return SessionSettings(
        enabled_extensions=("persona", "memory"),
        max_turns_per_run=1,
        max_requests_per_input=requests,
        extension_config=(
            ExtensionConfiguration(
                extension_id="memory", config_json=json.dumps({"extraction": extraction})
            ),
        ),
    )


def make(home, *, configuration=None, responses=None):
    faux = FauxProvider(
        responses if responses is not None else [FauxResponse.text("offline reply")] * 10
    )
    hub = create_application(
        home=home,
        models={
            "demo": ModelRegistration(
                Model("demo", "faux", "faux", max_tokens=2048), faux.stream, "offline"
            )
        },
        defaults=HostSettings(defaults=configuration or settings()),
        session_extensions=(PERSONA, MEMORY),
    )
    return hub, faux


async def new(hub, name="one"):
    return (await hub.create_session(ADMIN, title=name, request_id=name))["id"]


async def command(hub, identity, operation, arguments=None, revision=1):
    response = await hub.extension_command(
        ADMIN,
        identity,
        "memory",
        command=operation,
        arguments_json=json.dumps(arguments or {}, ensure_ascii=False),
        expected_revision=revision,
        request_id=uuid4().hex,
    )
    return json.loads(response["result_json"])


async def send(hub, identity, text):
    request_id = uuid4().hex
    await hub.submit_input(
        ADMIN, identity, text=text, request_id=request_id, idempotency_key=request_id
    )
    await hub.wait_for_idle(identity)


async def wait_job(hub, identity):
    # Finite scheduler turns, not an unbounded background poll or network dependency.
    for _ in range(1000):
        jobs = (await command(hub, identity, "jobs"))["jobs"]
        if jobs and all(job["status"] not in ("pending", "running") for job in jobs):
            return jobs
        await asyncio.sleep(0)
    pytest.fail("Memory job did not settle within the bounded scheduler window")


async def test_two_domains_three_sessions_management_recall_updates_and_restore(tmp_path):
    hub, faux = make(tmp_path)
    await hub.start()
    try:
        agent, another = await new(hub), await new(hub, "two")
        binding = ChannelBinding("fake-onebot", "bot", ADMIN.id, frozenset({"group:same"}))
        qq = hub.channel_session(binding, "group:same").id
        own = (
            await command(
                hub, agent, "remember", {"text": "Agent私有咖啡是冰美式", "kind": "preference"}
            )
        )["record"]
        other = (
            await command(hub, qq, "remember", {"text": "QQ群聊咖啡是拿铁", "kind": "preference"})
        )["record"]
        assert (await command(hub, another, "list"))["records"] == []
        assert [
            row["id"] for row in (await command(hub, agent, "list", {"query": "咖啡"}))["records"]
        ] == [own["id"]]
        for operation, args in (
            ("get", {"id": other["id"]}),
            (
                "update",
                {"id": other["id"], "expected_revision": other["revision"], "text": "forbidden"},
            ),
            ("forget", {"id": other["id"], "expected_revision": other["revision"]}),
            ("delete", {"id": other["id"], "expected_revision": other["revision"]}),
        ):
            with pytest.raises(NotFoundError):
                await command(hub, agent, operation, args)
        await send(hub, agent, "咖啡")
        request = faux.requests[0][1].messages
        assert "人设：月狐" in get_current_system_prompt(request)
        assert "Agent私有咖啡是冰美式" in str(request)
        assert "QQ群聊咖啡是拿铁" not in str(request)
        assert not get_current_tools(request)  # recall does not implicitly authorize tools
        assert len(faux.requests) == 1  # extraction is off by default
        updated = (
            await command(
                hub,
                agent,
                "update",
                {
                    "id": own["id"],
                    "expected_revision": own["revision"],
                    "text": "Agent私有咖啡改为浓缩",
                },
            )
        )["record"]
        with pytest.raises(ConflictError):
            await command(
                hub, agent, "delete", {"id": own["id"], "expected_revision": own["revision"]}
            )
        await send(hub, agent, "咖啡")
        assert "Agent私有咖啡改为浓缩" in str(faux.requests[-1][1].messages)
        assert "Agent私有咖啡是冰美式" not in str(faux.requests[-1][1].messages)
        await command(
            hub, agent, "forget", {"id": own["id"], "expected_revision": updated["revision"]}
        )
        assert (await command(hub, agent, "list"))["records"] == []
        assert (await command(hub, qq, "get", {"id": other["id"]}))["record"]["active"]
        await send(hub, agent, "咖啡")
        assert "Agent私有咖啡改为浓缩" not in str(faux.requests[-1][1].messages)
        assert len((await command(hub, agent, "list", {"include_forgotten": True}))["records"]) == 1
        await hub.update_session_config(
            ADMIN, agent, settings=SessionSettings(), expected_revision=1
        )
        await hub.wait_for_idle(agent)
        await send(hub, agent, "普通对话")
        assert "人设：月狐" not in get_current_system_prompt(faux.requests[-1][1].messages)
        assert not get_current_tools(faux.requests[-1][1].messages)
        # Disabled extensions remain manageable; no chat transcript entry is added by this read.
        before = (await hub.snapshot(ADMIN, agent))["messages"]
        await command(hub, agent, "list", {"include_forgotten": True}, revision=2)
        assert (await hub.snapshot(ADMIN, agent))["messages"] == before
    finally:
        await hub.aclose()
    restored, provider = make(tmp_path)
    await restored.start()
    try:
        assert not provider.requests
        assert (await command(restored, qq, "list"))["records"][0]["id"] == other["id"]
        assert len((await restored.snapshot(ADMIN, agent))["messages"]) == 8
    finally:
        await restored.aclose()


@pytest.mark.parametrize(
    "budget,expected_requests,expected_status", [(1, 1, "failed"), (2, 2, "done")]
)
async def test_real_host_gateway_meters_optional_extraction(
    tmp_path, budget, expected_requests, expected_status
):
    payload = json.dumps(
        {
            "candidates": [
                {
                    "text": "偏好无糖咖啡",
                    "kind": "preference",
                    "subject": "conversation",
                    "confidence": 0.8,
                }
            ]
        }
    )
    hub, faux = make(
        tmp_path,
        configuration=settings(extraction="model", requests=budget),
        responses=[FauxResponse.text("chat done"), FauxResponse.text(payload)],
    )
    await hub.start()
    try:
        identity = await new(hub)
        await send(hub, identity, "我喜欢无糖咖啡")
        jobs = await wait_job(hub, identity)
        assert jobs[0]["status"] == expected_status
        assert len(faux.requests) == expected_requests
        snapshot = await hub.snapshot(ADMIN, identity)
        assert snapshot["inputs"][0]["requests_used"] == expected_requests
        assert len(snapshot["messages"]) == 2
        assert "ticket" not in json.dumps(jobs)
        if budget == 2:
            record = (await command(hub, identity, "list"))["records"][0]
            assert record["source"]["origin"] == "extraction"
            assert (
                record["confidence"] == 0.6
            )  # Unverified candidates cannot self-promote confidence.
            assert not get_current_tools(faux.requests[-1][1].messages)
    finally:
        await hub.aclose()


def test_http_memory_commands_and_config_are_same_service(tmp_path):
    hub, faux = make(tmp_path, configuration=SessionSettings())
    auth = TokenAuth({"test-admin-token" * 3: ADMIN})
    with TestClient(create_server(hub, auth, allowed_origins=["http://testserver"])) as client:
        headers = {"Authorization": "Bearer " + "test-admin-token" * 3}
        identity = client.post(
            "/api/v1/sessions", headers=headers, json={"request_id": "new", "title": "E"}
        ).json()["id"]
        prefix = f"/api/v1/sessions/{identity}"
        config = settings().model_dump(mode="json")
        response = client.put(
            prefix + "/config",
            headers=headers,
            json={"request_id": "configure", "expected_revision": 1, "settings": config},
        )
        assert response.status_code == 200
        path = prefix + "/extensions/memory/commands/remember"
        body = {
            "request_id": "remember",
            "expected_revision": 2,
            "arguments_json": json.dumps({"text": "只属于当前会话"}),
        }
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200
        record = json.loads(response.json()["result_json"])["record"]
        assert record["source"]["origin"] == "manual"
        for override in ({"domain": "qq"}, {"scope_id": "other"}, {"session_id": "other"}):
            invalid = {**body, "arguments_json": json.dumps({"text": "unsafe", **override})}
            assert client.post(path, headers=headers, json=invalid).status_code == 422
        assert (
            client.post(path, headers=headers, json={**body, "expected_revision": 1}).status_code
            == 409
        )
        assert client.get(prefix, headers=headers).json()["messages"] == []
        assert not faux.requests
