"""Transport contract tests use an injected service port, not runtime/bootstrap state."""

import asyncio
import json
import threading
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from moonfox_app.config import HostSettings, SessionSettings
from moonfox_app.security.identity import (
    AccessDenied,
    AuthenticationError,
    Principal,
    TokenAuth,
)
from moonfox_app.server import _websocket
from moonfox_app.server.app import create_server
from moonfox_app.server.export import protocol_document, protocol_fixtures
from moonfox_app.services.errors import BudgetExceeded, ConflictError, NotFoundError, ResyncRequired

ORIGIN = "http://localhost:5173"
OWNER_TOKEN = "owner-" + "a" * 32
ADMIN_TOKEN = "admin-" + "b" * 32
OTHER_TOKEN = "other-" + "c" * 32
SECRET = "private-provider-secret-never-echo"
OWNER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
OTHER = {"Authorization": f"Bearer {OTHER_TOKEN}"}
SESSION = "/api/v1/sessions/session-demo"
WS = SESSION + "/events"
AUTH = TokenAuth(
    {
        OWNER_TOKEN: Principal("demo-user"),
        ADMIN_TOKEN: Principal("admin", is_admin=True),
        OTHER_TOKEN: Principal("other"),
    }
)


class FakeSubscription:
    def __init__(self, hub, events):
        self.hub = hub
        self.queue = asyncio.Queue()
        for event in events:
            self.queue.put_nowait(event)
        self.closed = threading.Event()
        self.active_next = False
        self.next_calls = 0

    async def next(self):
        self.active_next = True
        self.next_calls += 1
        try:
            if self.hub.overflow:
                self.hub.view.update(epoch="fresh-overflow", seq=99)
                raise ResyncRequired(SECRET)
            return await self.queue.get()
        finally:
            self.active_next = False

    def close(self):
        self.closed.set()


class FakeHub:
    def __init__(self):
        self.view = protocol_fixtures()["snapshot"]
        self.host_settings = HostSettings()
        self.host_revision = 0
        self.calls = []
        self.started = self.closed = 0
        self.running = True
        self.failure = None
        self.events = []
        self.subscriptions = []
        self.minimum_cursor = 0
        self.overflow = False
        self.receipts = {}

    def check(self, principal, session_id=None):
        if self.failure is not None:
            raise self.failure
        if session_id is not None:
            if session_id != self.view["session_id"]:
                raise NotFoundError(SECRET)
            if not principal.is_admin and principal.id != "demo-user":
                raise AccessDenied(SECRET)

    async def start(self):
        self.started += 1

    async def aclose(self):
        self.closed += 1

    async def list_sessions(self, principal):
        self.check(principal)
        return [deepcopy(self.view["session"])] if principal.id != "other" else []

    async def create_session(self, principal, *, title, request_id):
        self.check(principal)
        self.calls.append(("create", principal.id, title, request_id))
        return {**self.view["session"], "title": title, "owner_id": principal.id}

    async def snapshot(self, principal, session_id):
        self.check(principal, session_id)
        return deepcopy(self.view)

    async def submit_input(self, principal, session_id, *, text, request_id, idempotency_key):
        self.check(principal, session_id)
        self.calls.append(("input", principal.id, text, request_id, idempotency_key))
        if idempotency_key in self.receipts:
            previous_text, receipt = self.receipts[idempotency_key]
            if text != previous_text:
                raise ConflictError(SECRET)
            return receipt
        receipt = {"session_id": session_id, "request_id": request_id, "status": "queued"}
        self.receipts[idempotency_key] = (text, receipt)
        return receipt

    async def control(self, principal, session_id, *, action, request_id, input_id=None):
        self.check(principal, session_id)
        self.calls.append(("control", action, request_id, input_id))
        return {"session_id": session_id, "request_id": request_id, "status": "accepted"}

    async def update_session_config(self, principal, session_id, *, settings, expected_revision):
        self.check(principal, session_id)
        assert isinstance(settings, SessionSettings)
        if expected_revision != self.view["config_revision"]:
            raise ConflictError(SECRET)
        self.view["config_revision"] += 1
        self.view["settings"] = settings.model_dump(mode="json")
        self.calls.append(("session_config", expected_revision, settings))
        return {"revision": self.view["config_revision"], "settings": self.view["settings"]}

    async def config(self, principal):
        self.check(principal)
        assert principal.is_admin
        return {
            "revision": self.host_revision,
            "settings": self.host_settings.model_dump(mode="json"),
            "models": [{"id": "demo", "label": "离线演示"}],
            "tools": ["echo"],
            "extensions": [],
        }

    async def update_config(self, principal, *, settings, expected_revision):
        assert isinstance(settings, HostSettings)
        if expected_revision != self.host_revision:
            raise ConflictError(SECRET)
        self.host_revision += 1
        self.host_settings = settings
        return await self.config(principal)

    async def resolve_approval(self, principal, session_id, approval_id, *, decision):
        self.check(principal, session_id)
        self.calls.append(("approval", approval_id, decision))
        return {
            "id": approval_id,
            "session_id": session_id,
            "input_id": "input-demo",
            "tool_name": "echo",
            "arguments": {"text": "你好"},
            "status": "approved" if decision == "approve" else "denied",
            "expires_at": 1700000001000,
        }

    def subscribe(self, principal, session_id, *, epoch, after_seq):
        self.check(principal, session_id)
        self.calls.append(("subscribe", epoch, after_seq))
        if epoch != self.view["epoch"] or not self.minimum_cursor <= after_seq <= self.view["seq"]:
            raise ResyncRequired(SECRET)
        sub = FakeSubscription(self, [row for row in self.events if row["seq"] > after_seq])
        self.subscriptions.append(sub)
        return sub

    def seed_events(self):
        self.events = protocol_fixtures()["events"][:8]
        self.view["seq"] = self.events[-1]["seq"]


@pytest.fixture
def adapter():
    hub = FakeHub()
    app = create_server(hub, AUTH, allowed_origins=[ORIGIN], manage_lifecycle=False)
    with TestClient(app) as client:
        yield hub, client
    assert hub.started == hub.closed == 0


def subscribe(**overrides):
    return {
        "type": "subscribe",
        "token": OWNER_TOKEN,
        "epoch": "epoch-demo",
        "after_seq": 0,
        **overrides,
    }


def test_http_routes_and_dto_round_trips(adapter):
    hub, client = adapter
    assert client.get("/health").json() == {"status": "ok", "protocol_version": 1}
    assert client.get("/api/v1/me", headers=OWNER).json() == {"id": "demo-user", "is_admin": False}
    assert client.get("/api/v1/sessions", headers=OWNER).json()["sessions"] == [hub.view["session"]]
    assert client.get("/api/v1/sessions", headers=OTHER).json() == {"sessions": []}
    response = client.post(
        "/api/v1/sessions", headers=OWNER, json={"title": "中文测试", "request_id": "create-1"}
    )
    assert response.status_code == 201
    assert response.json()["title"] == "中文测试"
    assert ("create", "demo-user", "中文测试", "create-1") in hub.calls
    assert client.get(SESSION, headers=OWNER).json() == hub.view
    body = {"request_id": "input-1", "text": "你好", "idempotency_key": "once"}
    receipt = client.post(SESSION + "/inputs", headers=OWNER, json=body)
    assert receipt.status_code == 202
    assert receipt.json()["status"] == "queued"
    duplicate = client.post(SESSION + "/inputs", headers=OWNER, json={**body, "request_id": "dup"})
    assert duplicate.json() == receipt.json()
    assert ("input", "demo-user", "你好", "input-1", "once") in hub.calls
    conflict = client.post(SESSION + "/inputs", headers=OWNER, json={**body, "text": "不同"})
    assert conflict.status_code == 409
    for action in ("cancel", "continue", "compact"):
        command = {"request_id": action, "action": action, "input_id": "input-demo"}
        assert client.post(SESSION + "/control", headers=OWNER, json=command).status_code == 202
        assert ("control", action, action, "input-demo") in hub.calls
    for decision in ("approve", "deny"):
        response = client.post(
            SESSION + "/approvals/approval-demo",
            headers=OWNER,
            json={"request_id": decision, "decision": decision},
        )
        assert response.status_code == 200
        assert ("approval", "approval-demo", decision) in hub.calls
    response = client.get("/api/v1/protocol", headers=OWNER)
    assert response.json() == protocol_document()
    assert OWNER_TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_session_and_host_config_cas_and_strict_json_arrays(adapter):
    hub, client = adapter
    settings = SessionSettings(allowed_tools=("echo",), approval_tools=("echo",))
    command = {
        "request_id": "config",
        "expected_revision": 0,
        "settings": settings.model_dump(mode="json"),
    }
    response = client.put(SESSION + "/config", headers=OWNER, json=command)
    assert response.status_code == 200
    assert response.json() == {"revision": 1, "settings": settings.model_dump(mode="json")}
    assert client.get(SESSION, headers=OWNER).json()["config_revision"] == 1
    assert client.put(SESSION + "/config", headers=OWNER, json=command).status_code == 409
    config = client.get("/api/v1/config", headers=ADMIN)
    assert config.status_code == 200 and config.json()["revision"] == 0
    host = HostSettings(defaults=settings, max_concurrent_requests=3).model_dump(mode="json")
    command = {"request_id": "host", "expected_revision": 0, "settings": host}
    response = client.put("/api/v1/config", headers=ADMIN, json=command)
    assert response.status_code == 200
    assert response.json()["settings"] == host and response.json()["revision"] == 1
    assert hub.host_settings.max_concurrent_requests == 3
    assert client.put("/api/v1/config", headers=ADMIN, json=command).status_code == 409
    assert client.get("/api/v1/config", headers=OWNER).status_code == 403
    assert client.put("/api/v1/config", headers=OWNER, json=command).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/sessions"),
        ("POST", "/api/v1/sessions"),
        ("GET", SESSION),
        ("POST", SESSION + "/inputs"),
        ("POST", SESSION + "/control"),
        ("PUT", SESSION + "/config"),
        ("POST", SESSION + "/approvals/id"),
        ("GET", "/api/v1/config"),
        ("PUT", "/api/v1/config"),
        ("GET", "/api/v1/protocol"),
    ],
)
def test_all_api_routes_require_bearer(adapter, method, path):
    hub, client = adapter
    response = client.request(method, path, json={SECRET: OWNER_TOKEN})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert SECRET not in response.text and OWNER_TOKEN not in response.text
    assert not hub.calls


@pytest.mark.parametrize(
    "authorization", ["", "Basic " + OWNER_TOKEN, "Bearer " + SECRET, "Bearer  " + OWNER_TOKEN]
)
def test_invalid_credentials_are_generic(adapter, authorization):
    _, client = adapter
    response = client.get("/api/v1/me", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert SECRET not in response.text and OWNER_TOKEN not in response.text


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "null",
        "*",
        "https://evil.invalid",
        ORIGIN + ".evil",
        ORIGIN + "/path",
        "http://user:pass@localhost:5173",
    ],
)
def test_forbidden_http_origin_is_rejected_before_auth(adapter, origin):
    hub, client = adapter
    response = client.get(SESSION, headers={"Origin": origin})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert not hub.calls


def test_origin_normalization_cors_and_no_wildcard():
    hub = FakeHub()
    app = create_server(hub, AUTH, allowed_origins=["HTTP://LOCALHOST:80/"], manage_lifecycle=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/me", headers={**OWNER, "Origin": "http://localhost"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost"
        headers = {
            "Origin": "http://localhost",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        }
        response = client.options("/api/v1/config", headers=headers)
        assert response.status_code == 204
        assert "Authorization" in response.headers["access-control-allow-headers"]
        assert "Origin" in response.headers["vary"]
        assert "*" not in str(response.headers)
        for override in (
            {"Origin": "http://evil.invalid"},
            {"Access-Control-Request-Method": "DELETE"},
            {"Access-Control-Request-Headers": "X-Secret"},
        ):
            assert (
                client.options("/api/v1/config", headers={**headers, **override}).status_code == 403
            )
    for origin in (
        "*",
        "http://*.example.com",
        "null",
        "http://user:pass@localhost",
        "http://x/path",
    ):
        with pytest.raises(ValueError, match="explicit"):
            create_server(hub, AUTH, allowed_origins=[origin])


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", SESSION, None),
        ("POST", SESSION + "/inputs", {"request_id": "r", "text": "x", "idempotency_key": "i"}),
        ("POST", SESSION + "/control", {"request_id": "r", "action": "cancel"}),
        ("PUT", SESSION + "/config", {"request_id": "r", "expected_revision": 0, "settings": {}}),
        ("POST", SESSION + "/approvals/a", {"request_id": "r", "decision": "deny"}),
    ],
)
def test_session_access_denial_on_each_route(adapter, method, path, body):
    _, client = adapter
    response = client.request(method, path, headers=OTHER, json=body)
    assert response.status_code == 403 and SECRET not in response.text


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AuthenticationError, 401),
        (AccessDenied, 403),
        (NotFoundError, 404),
        (ConflictError, 409),
        (BudgetExceeded, 429),
        (RuntimeError, 500),
    ],
)
def test_exception_mapping_never_returns_internal_detail(adapter, error, status):
    hub, client = adapter
    hub.failure = error(SECRET)
    response = client.get(SESSION, headers={**OWNER, "Origin": ORIGIN})
    assert response.status_code == status
    assert SECRET not in response.text and "traceback" not in response.text.lower()
    assert response.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/sessions", {"request_id": "r", "owner_id": SECRET}),
        ("POST", "/api/v1/sessions", {"request_id": "r", "domain": "qq"}),
        ("POST", "/api/v1/sessions", {"request_id": 12, "title": SECRET}),
        ("POST", SESSION + "/inputs", {"request_id": "r", "text": 42, "idempotency_key": SECRET}),
        ("POST", SESSION + "/inputs", {"request_id": "r", "text": "", "idempotency_key": SECRET}),
        ("POST", SESSION + "/control", {"request_id": "r", "action": SECRET}),
        ("POST", SESSION + "/approvals/a", {"request_id": "r", "decision": SECRET}),
        (
            "PUT",
            SESSION + "/config",
            {"request_id": "r", "expected_revision": True, "settings": {}},
        ),
        ("PUT", SESSION + "/config", {"request_id": "r", "expected_revision": "0", "settings": {}}),
        ("PUT", SESSION + "/config", {"request_id": "r", "expected_revision": 0.0, "settings": {}}),
        (
            "PUT",
            SESSION + "/config",
            {"request_id": "r", "expected_revision": 0, "settings": {"max_turns_per_run": True}},
        ),
        (
            "PUT",
            SESSION + "/config",
            {
                "request_id": "r",
                "expected_revision": 0,
                "settings": {"max_tokens_per_input": "100"},
            },
        ),
        (
            "PUT",
            SESSION + "/config",
            {"request_id": "r", "expected_revision": 0, "settings": {"provider_key": SECRET}},
        ),
        (
            "PUT",
            "/api/v1/config",
            {
                "request_id": "r",
                "expected_revision": 0,
                "settings": {"max_concurrent_requests": False},
            },
        ),
    ],
)
def test_strict_requests_do_not_echo_validation_input(adapter, method, path, body):
    hub, client = adapter
    response = client.request(method, path, headers=ADMIN, json=body)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "detail": "Invalid request"}
    assert SECRET not in response.text and "input" not in response.json()
    assert not hub.calls


def test_malformed_oversize_chunked_and_url_credentials(adapter):
    hub, client = adapter
    for body in (b'{"token":"' + SECRET.encode(), b"\xff", b"[]"):
        response = client.post(
            "/api/v1/sessions", headers={**OWNER, "Content-Type": "application/json"}, content=body
        )
        assert response.status_code == 422 and SECRET not in response.text
    assert client.post("/api/v1/sessions", headers=OWNER, content="{}").status_code == 422
    for content in (b"x" * (128 * 1024 + 1), iter([b"x" * 65536, b"x" * 65537])):
        response = client.post("/api/v1/sessions", headers=OWNER, content=content)
        assert response.status_code == 413
    assert not hub.calls
    body = b'{"request_id":"boundary"}'.ljust(128 * 1024, b" ")
    assert (
        client.post(
            "/api/v1/sessions", headers={**OWNER, "Content-Type": "application/json"}, content=body
        ).status_code
        == 201
    )
    response = client.get("/api/v1/me?token=" + OWNER_TOKEN, headers=OWNER)
    assert response.status_code == 422 and OWNER_TOKEN not in response.text
    assert client.get("/api/v1/me?token=" + OWNER_TOKEN).status_code == 401


def test_response_dto_rejects_unexpected_internal_fields(adapter):
    hub, client = adapter
    hub.view["storage_path"] = SECRET
    response = client.get(SESSION, headers=OWNER)
    assert response.status_code == 500 and SECRET not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": ""},
        {"Origin": "null"},
        {"Origin": "http://evil.invalid"},
        {"Origin": ORIGIN + ".evil"},
    ],
)
def test_websocket_bad_origin_rejected_before_accept(adapter, headers):
    hub, client = adapter
    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(WS, headers=headers):
        pytest.fail("Origin must be checked before accept")
    assert error.value.code == 1008
    assert not hub.calls and not hub.subscriptions


def test_websocket_query_token_is_not_accepted(adapter):
    hub, client = adapter
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(WS + "?token=" + OWNER_TOKEN, headers={"Origin": ORIGIN}),
    ):
        pytest.fail("URL credentials must be rejected before accept")
    assert not hub.calls


@pytest.mark.parametrize(
    "command",
    [
        subscribe(token=SECRET),
        subscribe(token=""),
        subscribe(after_seq=True),
        subscribe(after_seq="0"),
        subscribe(after_seq=-1),
        subscribe(extra=SECRET),
        {"token": OWNER_TOKEN},
        {"type": "other", "token": OWNER_TOKEN},
    ],
)
def test_websocket_first_frame_validation_and_auth_are_non_echoing(adapter, command):
    hub, client = adapter
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(command)
        response = websocket.receive()
        assert response["type"] == "websocket.close" and response["code"] == 1008
        assert SECRET not in json.dumps(response) and OWNER_TOKEN not in json.dumps(response)
    assert not hub.calls and not hub.subscriptions


@pytest.mark.parametrize(
    ("frame", "binary", "code"),
    [
        ("not-json", False, 1008),
        ("中" * 3000, False, 1009),
        (b"{}", True, 1008),
        (b"x" * 8193, True, 1009),
    ],
)
def test_websocket_frame_type_and_utf8_byte_limit(adapter, frame, binary, code):
    _, client = adapter
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        (websocket.send_bytes if binary else websocket.send_text)(frame)
        response = websocket.receive()
        assert response["type"] == "websocket.close" and response["code"] == code


def test_websocket_auth_timeout(adapter, monkeypatch):
    hub, client = adapter
    monkeypatch.setattr(_websocket, "AUTH_TIMEOUT_SECONDS", 0.01)
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        assert websocket.receive()["code"] == 1008
    assert not hub.calls


def test_websocket_session_access_denied_before_replay_or_resync(adapter):
    hub, client = adapter
    hub.seed_events()
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(subscribe(token=OTHER_TOKEN, epoch="stale"))
        response = websocket.receive()
        assert response["type"] == "websocket.close" and response["code"] == 1008
    assert not hub.calls and not hub.subscriptions


async def assert_no_ws_tasks():
    tasks = [task for task in asyncio.all_tasks() if task.get_name().startswith("moonfox-ws-")]
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=1)
        assert not pending
    assert not any(task.get_name().startswith("moonfox-ws-") for task in asyncio.all_tasks())


def test_websocket_resume_disconnect_keeps_run_and_settles_owned_tasks(adapter):
    hub, client = adapter
    hub.seed_events()
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(subscribe(after_seq=3))
        received = [websocket.receive_json() for _ in range(5)]
        assert received == hub.events[3:]
        assert received[0]["payload"]["message"]["id"] == received[-2]["payload"]["message"]["id"]
    assert hub.subscriptions[0].closed.wait(1)
    client.portal.call(assert_no_ws_tasks)
    assert not hub.subscriptions[0].active_next
    assert hub.calls == [("subscribe", "epoch-demo", 3)]
    assert hub.running and hub.closed == 0


@pytest.mark.parametrize(
    ("epoch", "after_seq", "minimum"), [("old", 0, 0), ("epoch-demo", 9, 0), ("epoch-demo", 0, 4)]
)
def test_websocket_invalid_resume_gets_fresh_snapshot_cursor(adapter, epoch, after_seq, minimum):
    hub, client = adapter
    hub.seed_events()
    hub.minimum_cursor = minimum
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(subscribe(epoch=epoch, after_seq=after_seq))
        event = websocket.receive_json()
        assert event["type"] == "resync_required" and event["seq"] == 8
        assert event["epoch"] == "epoch-demo" and SECRET not in json.dumps(event)
        assert websocket.receive()["code"] == 1000
    client.portal.call(assert_no_ws_tasks)
    assert not hub.subscriptions and hub.running


def test_websocket_overflow_gets_fresh_cursor_and_unsubscribes(adapter):
    hub, client = adapter
    hub.overflow = True
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(subscribe())
        event = websocket.receive_json()
        assert event["type"] == "resync_required"
        assert event["epoch"] == "fresh-overflow" and event["seq"] == 99
        assert SECRET not in json.dumps(event)
        assert websocket.receive()["code"] == 1000
    assert hub.subscriptions[0].closed.wait(1)
    client.portal.call(assert_no_ws_tasks)
    assert hub.running


def test_heartbeat_does_not_advance_cursor_or_cancel_pending_next(adapter, monkeypatch):
    hub, client = adapter
    monkeypatch.setattr(_websocket, "HEARTBEAT_SECONDS", 0.01)
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(subscribe())
        first, second = websocket.receive_json(), websocket.receive_json()
        assert first == second
        assert first["type"] == "heartbeat" and first["seq"] == 0 and first["payload"] == {}
        assert hub.subscriptions[0].next_calls == 1
        websocket.send_json(subscribe())
        assert websocket.receive()["code"] == 1008
    assert hub.subscriptions[0].closed.wait(1)
    client.portal.call(assert_no_ws_tasks)
    assert hub.running and len(hub.subscriptions) == 1


def test_websocket_runtime_errors_are_generic(adapter):
    hub, client = adapter
    hub.failure = RuntimeError(SECRET)
    with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as websocket:
        websocket.send_json(subscribe())
        response = websocket.receive()
        assert response["code"] == 1011 and SECRET not in json.dumps(response)
    client.portal.call(assert_no_ws_tasks)


def test_explicit_lifespan_and_api_only_without_static(tmp_path):
    hub = FakeHub()
    app = create_server(hub, AUTH, allowed_origins=[], static_dir=tmp_path / "not-built")
    assert hub.started == hub.closed == 0
    with TestClient(app) as client:
        assert hub.started == 1 and hub.closed == 0
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/v1/me", headers=OWNER).status_code == 200
    assert hub.started == hub.closed == 1


def test_static_hosting_cannot_expose_siblings_or_shadow_api(tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<h1>Moonfox</h1>", encoding="utf-8")
    (static / "app.js").write_text("export const ready = true;", encoding="utf-8")
    (static / "api").mkdir()
    (static / "api" / "unknown").write_text(SECRET, encoding="utf-8")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    hub = FakeHub()
    app = create_server(
        hub, AUTH, allowed_origins=[ORIGIN], static_dir=static, manage_lifecycle=False
    )
    with TestClient(app) as client:
        assert client.get("/").text == "<h1>Moonfox</h1>"
        assert client.get("/app.js").status_code == 200
        for path in (
            "/%2e%2e/secret.txt",
            "/..%5csecret.txt",
            "/%2e%2e%2fsecret.txt",
            "/missing",
            "/api/unknown",
        ):
            response = client.get(path, headers=OWNER)
            assert response.status_code == 404 and SECRET not in response.text
        assert client.get("/api/unknown").status_code == 401
        assert client.get(SESSION, headers=OWNER).status_code == 200
        assert client.get("/", headers={"Origin": "http://evil.invalid"}).status_code == 403
