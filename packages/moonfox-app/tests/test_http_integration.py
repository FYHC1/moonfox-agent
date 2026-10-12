import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from moonfox_agent.api import AgentTool, AgentToolResult
from moonfox_ai.api import FauxProvider, FauxResponse, Model, TextContent
from moonfox_app.api import (
    HostSettings,
    ModelRegistration,
    Principal,
    SessionSettings,
    TokenAuth,
    create_application,
)
from moonfox_app.server.app import create_server
from moonfox_app.server.schema import Snapshot, WireEvent

TOKEN = "test-owner-token-for-integration-only"
OTHER_TOKEN = "test-other-token-for-integration-only"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Origin": "http://testserver"}


def application(tmp_path, responses, *, settings=None, tools=()):
    faux = FauxProvider(responses)
    hub = create_application(
        home=tmp_path,
        models={
            "demo": ModelRegistration(
                Model("demo", "faux", "faux", max_tokens=100), faux.stream, "Test"
            )
        },
        defaults=HostSettings(defaults=settings or SessionSettings()),
        tools=tools,
    )
    app = create_server(
        hub,
        TokenAuth({TOKEN: Principal("owner", True), OTHER_TOKEN: Principal("other")}),
        allowed_origins=("http://testserver",),
    )
    return app, faux


def new_session(client):
    response = client.post(
        "/api/v1/sessions", headers=HEADERS, json={"request_id": "create", "title": "测试"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def snapshot(client, identity):
    response = client.get(f"/api/v1/sessions/{identity}", headers=HEADERS)
    assert response.status_code == 200, response.text
    Snapshot.model_validate_json(response.content)
    return response.json()


def send(client, identity, key="input"):
    return client.post(
        f"/api/v1/sessions/{identity}/inputs",
        headers=HEADERS,
        json={"request_id": key, "idempotency_key": key, "text": "你好"},
    )


def wait_for(client, identity, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = snapshot(client, identity)
        if predicate(value):
            return value
        time.sleep(0.01)
    pytest.fail("Session did not reach expected state")


def test_real_websocket_disconnect_does_not_cancel_agent(tmp_path):
    app, faux = application(
        tmp_path, [FauxResponse.text("background response", delay_ms=10, chunk_size=1)]
    )
    with TestClient(app) as client:
        identity = new_session(client)
        initial = snapshot(client, identity)
        with client.websocket_connect(
            f"/api/v1/sessions/{identity}/events", headers={"Origin": "http://testserver"}
        ) as websocket:
            websocket.send_json(
                {
                    "type": "subscribe",
                    "token": TOKEN,
                    "epoch": initial["epoch"],
                    "after_seq": initial["seq"],
                }
            )
            response = send(client, identity)
            assert response.status_code == 202 and response.json()["status"] == "pending"
            event = websocket.receive_json()
            WireEvent.model_validate(event)
            assert event["type"] == "input_received"
        settled = wait_for(
            client, identity, lambda value: value["inputs"][-1]["status"] == "completed"
        )
        assert settled["messages"][-1]["text"] == "background response"
        assert len(faux.requests) == 1
        assert all("scope_id" not in str(message) for message in settled["messages"])


def test_live_event_protocol_stable_message_ids_and_resume_window(tmp_path):
    app, _ = application(tmp_path, [FauxResponse.text("streamed", delay_ms=4, chunk_size=2)])
    with TestClient(app) as client:
        identity = new_session(client)
        initial = snapshot(client, identity)
        events = []
        with client.websocket_connect(
            f"/api/v1/sessions/{identity}/events", headers={"Origin": "http://testserver"}
        ) as websocket:
            websocket.send_json(
                {"type": "subscribe", "token": TOKEN, "epoch": initial["epoch"], "after_seq": 0}
            )
            assert send(client, identity).status_code == 202
            while True:
                event = websocket.receive_json()
                WireEvent.model_validate(event)
                events.append(event)
                if event["type"] == "run_finished":
                    break
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert "partial" not in str(events) and "thinking" not in str(events)
        starts = [e["payload"]["message"] for e in events if e["type"] == "message_start"]
        ends = [e["payload"]["message"] for e in events if e["type"] == "message_end"]
        assert [m["id"] for m in starts] == [m["id"] for m in ends]
        final = snapshot(client, identity)
        assert [m["id"] for m in final["messages"]] == [m["id"] for m in ends]
        with client.websocket_connect(
            f"/api/v1/sessions/{identity}/events", headers={"Origin": "http://testserver"}
        ) as websocket:
            websocket.send_json(
                {"token": TOKEN, "epoch": initial["epoch"], "after_seq": events[-2]["seq"]}
            )
            assert websocket.receive_json() == events[-1]


def test_real_configuration_cas_and_unauthorized_reads(tmp_path):
    app, _ = application(tmp_path, [])
    with TestClient(app) as client:
        identity = new_session(client)
        initial = snapshot(client, identity)
        settings = initial["settings"] | {"max_turns_per_run": 1}
        command = {
            "request_id": "settings",
            "expected_revision": initial["config_revision"],
            "settings": settings,
        }
        response = client.put(f"/api/v1/sessions/{identity}/config", headers=HEADERS, json=command)
        assert response.status_code == 200, response.text
        assert (
            client.put(
                f"/api/v1/sessions/{identity}/config", headers=HEADERS, json=command
            ).status_code
            == 409
        )
        denied = client.get(
            f"/api/v1/sessions/{identity}", headers={"Authorization": f"Bearer {OTHER_TOKEN}"}
        )
        assert denied.status_code == 403
        assert (
            client.post(
                "/api/v1/sessions", headers=HEADERS, json={"request_id": "bad", "domain": "qq"}
            ).status_code
            == 422
        )
        assert (
            client.get("/api/v1/config", headers=HEADERS).json()["settings"]["defaults"]["model_id"]
            == "demo"
        )


def test_real_tool_approval_http_is_single_use(tmp_path):
    class Args(BaseModel):
        pass

    executed = []

    async def execute(call_id, args, signal, update):
        executed.append(True)
        return AgentToolResult([TextContent("approved")])

    app, _ = application(
        tmp_path,
        [FauxResponse.tool_call("t", {}), FauxResponse.text("finished")],
        tools=(AgentTool("t", "", "", Args, execute=execute),),
        settings=SessionSettings(allowed_tools=("t",), approval_tools=("t",)),
    )
    with TestClient(app) as client:
        identity = new_session(client)
        assert send(client, identity).status_code == 202
        pending = wait_for(client, identity, lambda value: bool(value["approvals"]))
        approval_id = pending["approvals"][0]["id"]
        path = f"/api/v1/sessions/{identity}/approvals/{approval_id}"
        command = {"request_id": "approve", "decision": "approve"}
        assert (
            client.post(
                path, headers={"Authorization": f"Bearer {OTHER_TOKEN}"}, json=command
            ).status_code
            == 403
        )
        assert executed == []
        assert client.post(path, headers=HEADERS, json=command).status_code == 200
        final = wait_for(
            client, identity, lambda value: value["inputs"][-1]["status"] == "completed"
        )
        assert executed == [True] and final["approvals"] == []
        assert client.post(path, headers=HEADERS, json=command).status_code == 409
