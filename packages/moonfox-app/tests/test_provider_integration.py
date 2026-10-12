"""Management and real loopback HTTP acceptance; never contacts a paid provider."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from moonfox_agent.api import AbortToken, AgentTool, AgentToolResult
from moonfox_ai.api import (
    AnthropicProvider,
    Context,
    ImageContent,
    Model,
    OpenAIProvider,
    OpenAIResponsesProvider,
    StreamOptions,
    TextContent,
    TextDelta,
    UserMessage,
    normalize_context,
    now_ms,
)
from moonfox_app.api import (
    GenerationParameters,
    HostSettings,
    ModelSettings,
    Principal,
    ProviderSettings,
    SessionSettings,
    TokenAuth,
    create_application,
)
from moonfox_app.extension_api import ChannelInput
from moonfox_app.security.secrets import SecretStorageError
from moonfox_app.server.app import create_server
from moonfox_app.services.channel import ChannelBinding
from moonfox_app.services.http_transport import HttpxTransport

OWNER = Principal("owner", True)
TOKEN = "synthetic-management-admin-token"
READER_TOKEN = "synthetic-management-reader-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class Protector:
    """Synthetic test-only storage, not a production cryptography implementation."""

    kind = "test-only"

    def protect(self, value, *, context):
        return (context + "|" + value[::-1]).encode()

    def unprotect(self, value, *, context):
        prefix, text = value.decode().split("|", 1)
        if prefix != context:
            raise SecretStorageError("wrong context")
        return text[::-1]


def sse(data, event=None):
    prefix = f"event: {event}\r\n" if event else ""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return (prefix + "data: " + text + "\r\n\r\n").encode()


def openai_frames(*, tool=False):
    def frame(delta, finish=None):
        return sse(
            {
                "id": "reply-1",
                "model": "remote",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish},
                ],
            }
        )

    if tool:
        parts = [
            frame(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "test.echo", "arguments": "{"},
                        }
                    ],
                }
            ),
            frame({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, "tool_calls"),
        ]
    else:
        parts = [
            frame({"role": "assistant", "content": "本地"}),
            frame({"content": "回复"}),
            frame({}, "stop"),
        ]
    return parts + [
        sse(
            {
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            }
        ),
        sse("[DONE]"),
    ]


def anthropic_frames(*, tool=False):
    def frame(kind, **values):
        return sse({"type": kind, **values}, kind)

    parts = [
        frame(
            "message_start",
            message={
                "id": "reply-1",
                "role": "assistant",
                "model": "remote",
                "content": [],
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        )
    ]
    if tool:
        parts += [
            frame(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "test.echo",
                    "input": {},
                },
            ),
            frame(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": "{"},
            ),
            frame(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": "}"},
            ),
        ]
    else:
        parts += [
            frame("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "本地"}),
            frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "回复"}),
        ]
    return parts + [
        frame("content_block_stop", index=0),
        frame(
            "message_delta",
            delta={"stop_reason": "tool_use" if tool else "end_turn"},
            usage={"output_tokens": 3},
        ),
        frame("message_stop"),
    ]


def responses_frames(*, tool=False):
    def frame(kind, **values):
        return sse({"type": kind, **values}, kind)

    identity = {"id": "reply-1", "model": "remote"}
    parts = [
        frame("response.created", response={**identity, "status": "in_progress", "output": []})
    ]
    if tool:
        item = {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "test.echo",
            "arguments": "{}",
            "status": "completed",
        }
        ref = {"item_id": "fc-1", "output_index": 0}
        parts += [
            frame(
                "response.output_item.added",
                output_index=0,
                item={**item, "arguments": "", "status": "in_progress"},
            ),
            frame("response.function_call_arguments.delta", **ref, delta="{"),
            frame("response.function_call_arguments.delta", **ref, delta="}"),
            frame("response.function_call_arguments.done", **ref, arguments="{}"),
        ]
    else:
        content = {"type": "output_text", "text": "本地回复", "annotations": []}
        item = {
            "type": "message",
            "id": "msg-1",
            "role": "assistant",
            "status": "completed",
            "content": [content],
        }
        ref = {"item_id": "msg-1", "output_index": 0, "content_index": 0}
        parts += [
            frame(
                "response.output_item.added",
                output_index=0,
                item={**item, "content": [], "status": "in_progress"},
            ),
            frame("response.content_part.added", **ref, part={**content, "text": ""}),
            frame("response.output_text.delta", **ref, delta="本地"),
            frame("response.output_text.delta", **ref, delta="回复"),
            frame("response.output_text.done", **ref, text="本地回复"),
            frame("response.content_part.done", **ref, part=content),
        ]
    return parts + [
        frame("response.output_item.done", output_index=0, item=item),
        frame(
            "response.completed",
            response={
                **identity,
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        ),
    ]


@pytest.fixture
def remote():
    state = SimpleNamespace(
        requests=[],
        tool=False,
        gate=False,
        before_headers=False,
        status=200,
        release=threading.Event(),
        entered=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # Never log request headers, paths or payloads.

        def do_GET(self):
            state.requests.append((self.path, dict(self.headers), None))
            data = json.dumps({"data": [{"id": "remote", "display_name": "本地测试模型"}]}).encode()
            self.send_response(state.status)
            if state.status == 302:
                self.send_header("Location", state.url + "/credential-sink")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, dict(self.headers), data))
            state.entered.set()
            if state.before_headers:
                state.release.wait(5)
            is_anthropic = self.path.endswith("/messages")
            is_responses = self.path.endswith("/responses")
            has_result = any(
                item.get("role") == "tool"
                or item.get("type") == "function_call_output"
                or any(
                    block.get("type") == "tool_result"
                    for block in (item.get("content") or [])
                    if isinstance(block, dict)
                )
                for item in data["input" if is_responses else "messages"]
            )
            factory = (
                responses_frames
                if is_responses
                else anthropic_frames
                if is_anthropic
                else openai_frames
            )
            frames = factory(tool=state.tool and not has_result)
            if data.get("tools"):
                declaration = data["tools"][0]
                wire_name = (
                    declaration["name"]
                    if is_anthropic or is_responses
                    else declaration["function"]["name"]
                )
                assert "." not in wire_name and len(wire_name) <= 64
                frames = [part.replace(b"test.echo", wire_name.encode()) for part in frames]
            self.send_response(state.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for index, chunk in enumerate(frames):
                    # Tiny writes exercise real incremental decoding, not a fully buffered body.
                    midpoint = len(chunk) // 2
                    self.wfile.write(chunk[:midpoint])
                    self.wfile.flush()
                    self.wfile.write(chunk[midpoint:])
                    self.wfile.flush()
                    if state.gate and index == (3 if is_responses else 2 if is_anthropic else 0):
                        state.release.wait(5)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


async def configure(hub, remote, *, protocol="openai", params=None, tools=True):
    provider = ProviderSettings(name="本地服务", protocol=protocol, base_url=remote.url)
    model = ModelSettings(
        provider_id="local",
        name="本地模型",
        remote_id="remote",
        max_output_tokens=1024,
        supports_tools=tools,
        supports_reasoning=protocol == "openai-responses",
        parameters=params or GenerationParameters(),
    )
    await hub.put_provider(
        OWNER,
        "local",
        settings=provider,
        expected_revision=0,
        secret_action="replace",
        api_key="synthetic-old-key",
    )
    await hub.put_model(OWNER, "local-model", settings=model, expected_revision=0)
    catalog = await hub.managed_models(OWNER)
    await hub.set_default_model(OWNER, "local-model", expected_revision=catalog["config_revision"])
    return provider, model


def command(revision=0, **values):
    return {"request_id": "test", "expected_revision": revision, **values}


def test_http_management_empty_host_permissions_cas_and_no_key_echo(tmp_path, remote):
    hub = create_application(home=tmp_path, models={}, secret_protector=Protector())
    app = create_server(
        hub,
        TokenAuth({TOKEN: OWNER, READER_TOKEN: Principal("reader")}),
        allowed_origins=("http://testserver",),
    )
    key = "synthetic-private-api-key"
    provider = ProviderSettings(name="测试", base_url=remote.url).model_dump(mode="json")
    with TestClient(app) as client:
        reader = {"Authorization": f"Bearer {READER_TOKEN}"}
        paths = [
            ("GET", "/providers"),
            ("PUT", "/providers/local"),
            ("POST", "/providers/local/test"),
            ("POST", "/providers/local/delete"),
            ("GET", "/models"),
            ("POST", "/models"),
            ("PUT", "/models/local-model"),
            ("POST", "/models/local-model/delete"),
            ("POST", "/models/local-model/default"),
        ]
        for method, suffix in paths:
            result = client.request(
                method, "/api/v1" + suffix, headers=reader, json={"api_key": key}
            )
            assert result.status_code == 403 and key not in result.text
        assert client.get("/api/v1/providers").status_code == 401
        assert client.get("/api/v1/models", headers=HEADERS).json()["models"] == []
        assert (
            client.post("/api/v1/sessions", headers=HEADERS, json={"request_id": "new"}).status_code
            == 422
        )
        put = command(settings=provider, secret_action="replace", api_key=key)
        result = client.put("/api/v1/providers/local", headers=HEADERS, json=put)
        assert result.status_code == 200 and result.json()["key_configured"]
        assert key not in result.text
        assert client.put("/api/v1/providers/local", headers=HEADERS, json=put).status_code == 409
        assert key not in client.get("/api/v1/providers", headers=HEADERS).text
        invalid = command(1, settings=provider, api_key=key, secret_action="keep")
        result = client.put("/api/v1/providers/local", headers=HEADERS, json=invalid)
        assert result.status_code == 422 and key not in result.text
        invalid = command(
            1, settings=provider | {"base_url": "https://user:" + key + "@example.test"}
        )
        result = client.put("/api/v1/providers/local", headers=HEADERS, json=invalid)
        assert result.status_code == 422 and key not in result.text
        probe = client.post("/api/v1/providers/local/test", headers=HEADERS, json=command(1))
        assert probe.status_code == 200 and probe.json()["ok"]
        assert probe.json()["models"][0]["id"] == "remote" and key not in probe.text
        model = ModelSettings(provider_id="local", remote_id="remote", name="模型")
        path = "/api/v1/models/local-model"
        result = client.put(
            path, headers=HEADERS, json=command(settings=model.model_dump(mode="json"))
        )
        assert result.status_code == 200 and result.json()["available"]
        assert (
            client.post(
                "/api/v1/providers/local/delete", headers=HEADERS, json=command(1)
            ).status_code
            == 409
        )
        catalog = client.get("/api/v1/models", headers=HEADERS).json()
        revision = catalog["config_revision"]
        result = client.post(path + "/default", headers=HEADERS, json=command(revision))
        assert (
            result.status_code == 200
            and result.json()["settings"]["defaults"]["model_id"] == "local-model"
        )
        assert (
            client.post(path + "/default", headers=HEADERS, json=command(revision)).status_code
            == 409
        )
        assert client.post(path + "/delete", headers=HEADERS, json=command(1)).status_code == 409
        created = client.post("/api/v1/sessions", headers=HEADERS, json={"request_id": "new"})
        assert created.status_code == 201 and created.json()["model_id"] == "local-model"
        # A protocol-incompatible combination is rejected at the service boundary (422, not 500).
        invalid_model = model.model_copy(update={"supports_reasoning": False})
        assert (
            client.put(
                path,
                headers=HEADERS,
                json=command(1, settings=invalid_model.model_dump(mode="json")),
            ).status_code
            == 200
        )
        snapshot = client.get("/api/v1/sessions/" + created.json()["id"], headers=HEADERS).json()
        bad_settings = snapshot["settings"] | {"thinking_level": "high"}
        result = client.put(
            "/api/v1/sessions/" + created.json()["id"] + "/config",
            headers=HEADERS,
            json=command(snapshot["config_revision"], settings=bad_settings),
        )
        assert result.status_code == 422
        assert key not in json.dumps(snapshot)
    assert all(
        key.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert len(remote.requests) == 1 and remote.requests[0][0] == "/v1/models"


@pytest.mark.parametrize("protocol", ["openai", "openai-responses", "anthropic"])
async def test_real_http_tool_continuation_uses_model_defaults_and_session_overrides(
    tmp_path, remote, protocol
):
    remote.tool = True
    calls = []

    class Args(BaseModel):
        pass

    async def execute(call_id, args, signal, update):
        calls.append(call_id)
        return AgentToolResult([TextContent("tool-success")])

    settings = SessionSettings(allowed_tools=("test.echo",))
    hub = create_application(
        home=tmp_path,
        models={},
        secret_protector=Protector(),
        defaults=HostSettings(defaults=settings),
        tools=(AgentTool("test.echo", "test", "test", Args, execute=execute),),
    )
    await hub.start()
    try:
        await configure(
            hub,
            remote,
            protocol=protocol,
            params=GenerationParameters(
                temperature=0.8,
                max_tokens=128,
                stop=None if protocol == "openai-responses" else ("stop",),
            ),
        )
        identity = (await hub.create_session(OWNER, title="test", request_id="new"))["id"]
        snapshot = await hub.snapshot(OWNER, identity)
        current = SessionSettings.model_validate_json(json.dumps(snapshot["settings"]))
        await hub.update_session_config(
            OWNER,
            identity,
            expected_revision=snapshot["config_revision"],
            settings=current.model_copy(
                update={
                    "generation": GenerationParameters(
                        temperature=0.0, stop=None if protocol == "openai-responses" else ()
                    ),
                    "thinking_level": "high" if protocol == "openai-responses" else "off",
                }
            ),
        )
        await hub.submit_input(
            OWNER, identity, text="use tool", request_id="one", idempotency_key="one"
        )
        await hub.wait_for_idle()
        final = await hub.snapshot(OWNER, identity)
        assert final["inputs"][-1]["status"] == "completed", final["inputs"]
        assert final["messages"][-1]["text"] == "本地回复" and calls == ["call-1"]
        assert len(remote.requests) == 2
        for path, headers, data in remote.requests:
            assert (
                path
                == {
                    "openai": "/v1/chat/completions",
                    "openai-responses": "/v1/responses",
                    "anthropic": "/v1/messages",
                }[protocol]
            )
            token_field = "max_output_tokens" if protocol == "openai-responses" else "max_tokens"
            assert (
                data["temperature"] == 0 and data[token_field] == 128 and data["model"] == "remote"
            )
            assert not data.get("stop_sequences" if protocol == "anthropic" else "stop")
            assert data["tools"][0]["function" if protocol == "openai" else "name"]
            if protocol == "openai-responses":
                assert (
                    data["store"] is False and "max_tokens" not in data and "messages" not in data
                )
                assert data["reasoning"] == {"effort": "high"}
            assert headers["x-api-key" if protocol == "anthropic" else "Authorization"].endswith(
                "synthetic-old-key"
            )
        assert "tool-success" in json.dumps(
            remote.requests[1][2]["input" if protocol == "openai-responses" else "messages"]
        )
    finally:
        await hub.aclose()


@pytest.mark.parametrize("protocol", ["openai", "openai-responses"])
async def test_edits_apply_only_next_run_and_disabled_model_retains_history(
    tmp_path, remote, protocol
):
    remote.gate = True
    hub = create_application(home=tmp_path, models={}, secret_protector=Protector())
    await hub.start()
    try:
        provider, model = await configure(
            hub, remote, protocol=protocol, params=GenerationParameters(temperature=0.2)
        )
        identity = (await hub.create_session(OWNER, title="test", request_id="new"))["id"]
        await hub.submit_input(
            OWNER, identity, text="first", request_id="one", idempotency_key="one"
        )
        await asyncio.wait_for(asyncio.to_thread(remote.entered.wait, 3), 4)
        assert remote.requests
        await hub.put_provider(
            OWNER,
            "local",
            settings=provider,
            expected_revision=1,
            secret_action="replace",
            api_key="synthetic-new-key",
        )
        await hub.put_model(
            OWNER,
            "local-model",
            expected_revision=1,
            settings=model.model_copy(update={"parameters": GenerationParameters(temperature=0.7)}),
        )
        remote.release.set()
        await hub.wait_for_idle()
        await hub.submit_input(
            OWNER, identity, text="second", request_id="two", idempotency_key="two"
        )
        await hub.wait_for_idle()
        assert [item[2]["temperature"] for item in remote.requests] == [0.2, 0.7]
        assert [item[1]["Authorization"] for item in remote.requests] == [
            "Bearer synthetic-old-key",
            "Bearer synthetic-new-key",
        ]
        await hub.put_provider(
            OWNER,
            "local",
            settings=provider.model_copy(update={"enabled": False}),
            expected_revision=2,
        )
        await hub.submit_input(
            OWNER, identity, text="disabled", request_id="three", idempotency_key="three"
        )
        await hub.wait_for_idle()
        final = await hub.snapshot(OWNER, identity)
        assert final["inputs"][-1]["status"] == "error" and len(remote.requests) == 2
        assert len(final["messages"]) == 4
    finally:
        remote.release.set()
        await hub.aclose()
    hub = create_application(home=tmp_path, models={}, secret_protector=Protector())
    await hub.start()
    try:
        restored = await hub.snapshot(OWNER, identity)
        assert restored["messages"] == final["messages"]
        assert not (await hub.managed_models(OWNER))["models"][0]["available"]
    finally:
        await hub.aclose()


@pytest.mark.parametrize("protocol", ["openai", "openai-responses"])
async def test_budget_caps_output_and_channel_uses_same_catalog_with_isolated_history(
    tmp_path, remote, protocol
):
    hub = create_application(
        home=tmp_path,
        models={},
        secret_protector=Protector(),
        defaults=HostSettings(defaults=SessionSettings(max_tokens_per_input=300)),
    )
    await hub.start()
    try:
        await configure(
            hub,
            remote,
            protocol=protocol,
            params=GenerationParameters(max_tokens=1024),
            tools=False,
        )
        agent = (await hub.create_session(OWNER, title="agent", request_id="agent"))["id"]
        gateway = hub.bind_channel(
            ChannelBinding("connection", "bot", "owner", frozenset({"group:one"}))
        )
        receipt = await gateway.submit(
            ChannelInput("connection", "bot", "group:one", "user", "one", now_ms(), "hello")
        )
        await hub.wait_for_idle()
        assert receipt.session_id != agent
        snapshot = await hub.snapshot(OWNER, receipt.session_id)
        assert (
            snapshot["session"]["domain"] == "qq"
            and snapshot["session"]["model_id"] == "local-model"
        )
        assert snapshot["messages"][-1]["text"] == "本地回复"
        assert (await hub.snapshot(OWNER, agent))["messages"] == []
        data = remote.requests[0][2]
        token_field = "max_output_tokens" if protocol == "openai-responses" else "max_tokens"
        assert 0 < data[token_field] < 300 and "tools" not in data
    finally:
        await hub.aclose()


@pytest.mark.parametrize("protocol", ["openai", "openai-responses", "anthropic"])
async def test_real_transport_streams_small_frames_before_completion_and_images(remote, protocol):
    remote.gate = True
    provider_type = {
        "openai": OpenAIProvider,
        "openai-responses": OpenAIResponsesProvider,
        "anthropic": AnthropicProvider,
    }[protocol]
    provider = provider_type(HttpxTransport(), base_url=remote.url, supports_images=True)
    context = normalize_context(
        Context([UserMessage([TextContent("image"), ImageContent("eA==", "image/png")], now_ms())])
    )
    stream = provider.stream(Model("remote", "local", protocol), context)
    try:
        async with asyncio.timeout(3):
            async for event in stream:
                if isinstance(event, TextDelta) and not remote.release.is_set():
                    assert event.delta == "本地" and not remote.release.is_set()
                    remote.release.set()
        result = await stream.result()
        assert result.stop_reason == "stop" and result.usage.effective_total == 15
        assert result.content[0].text == "本地回复"
        data = remote.requests[0][2]
        assert "eA==" in json.dumps(data["input" if protocol == "openai-responses" else "messages"])
    finally:
        remote.release.set()
        await stream.aclose()


@pytest.mark.parametrize("protocol", ["openai", "openai-responses"])
@pytest.mark.parametrize("before_headers", [False, True])
async def test_real_transport_cancellation_closes_http_client(
    remote, monkeypatch, before_headers, protocol
):
    clients = []
    original = httpx.AsyncClient

    class TrackedClient(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", TrackedClient)
    remote.gate = not before_headers
    remote.before_headers = before_headers
    token = AbortToken()
    provider_type = OpenAIResponsesProvider if protocol == "openai-responses" else OpenAIProvider
    provider = provider_type(HttpxTransport(), base_url=remote.url)
    stream = provider.stream(
        Model("remote", "local", "openai"),
        normalize_context(Context([])),
        StreamOptions(signal=token),
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(remote.entered.wait, 3), 4)
        token.abort()
        result = await asyncio.wait_for(stream.result(), 3)
        assert result.stop_reason == "aborted"
        await stream.aclose()
        assert clients and all(client.is_closed for client in clients)
    finally:
        remote.release.set()
        await stream.aclose()


async def test_discovery_does_not_follow_redirects_or_forward_credentials(tmp_path, remote):
    remote.status = 302
    hub = create_application(home=tmp_path, models={}, secret_protector=Protector())
    await hub.start()
    try:
        await configure(hub, remote)
        result = await hub.test_provider(OWNER, "local", expected_revision=1)
        assert result["status"] == "unavailable"
        assert len(remote.requests) == 1 and remote.requests[0][0] == "/v1/models"
        assert "synthetic-old-key" not in str(result)
    finally:
        await hub.aclose()


def test_http_auto_ids_responses_creation_manual_unicode_ids_and_stable_edits(tmp_path, remote):
    hub = create_application(home=tmp_path, models={}, secret_protector=Protector())
    app = create_server(hub, TokenAuth({TOKEN: OWNER}), allowed_origins=("http://testserver",))
    with TestClient(app) as client:
        provider = ProviderSettings(
            name="Responses", protocol="openai-responses", base_url=remote.url, auth_mode="none"
        )
        result = client.put(
            "/api/v1/providers/local",
            headers=HEADERS,
            json=command(settings=provider.model_dump(mode="json")),
        )
        assert (
            result.status_code == 200
            and result.json()["settings"]["protocol"] == "openai-responses"
        )
        model = ModelSettings(
            provider_id="local", name="gpt-4.1", remote_id="gpt-4.1", supports_reasoning=True
        )
        settings = model.model_dump(mode="json")
        for expected in ("gpt-4.1", "local-gpt-4.1", "local-gpt-4.1-2"):
            created = client.post(
                "/api/v1/models", headers=HEADERS, json=command(settings=settings)
            )
            assert created.status_code == 200 and created.json()["id"] == expected
        assert (
            client.post(
                "/api/v1/models", headers=HEADERS, json=command(1, settings=settings)
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/api/v1/models/gpt-4.1", headers=HEADERS, json=command(settings=settings)
            ).status_code
            == 409
        )
        changed = client.put(
            "/api/v1/models/gpt-4.1",
            headers=HEADERS,
            json=command(1, settings={**settings, "name": "Display name changed"}),
        )
        assert changed.status_code == 200 and changed.json()["id"] == "gpt-4.1"
        manual = "手工-GPT-4.1"
        path = "/api/v1/models/" + quote(manual, safe="")
        saved = client.put(path, headers=HEADERS, json=command(settings=settings))
        assert saved.status_code == 200 and saved.json()["id"] == manual
        catalog = client.get("/api/v1/models", headers=HEADERS).json()
        assert len(catalog["models"]) == 4
        default = client.post(
            path + "/default", headers=HEADERS, json=command(catalog["config_revision"])
        )
        assert (
            default.status_code == 200
            and default.json()["settings"]["defaults"]["model_id"] == manual
        )
        session = client.post(
            "/api/v1/sessions", headers=HEADERS, json={"request_id": "new"}
        ).json()
        assert session["model_id"] == manual
        assert client.post(path + "/delete", headers=HEADERS, json=command(1)).status_code == 409
        invalid = {**settings, "parameters": {"stop": []}}
        assert (
            client.post(
                "/api/v1/models", headers=HEADERS, json=command(settings=invalid)
            ).status_code
            == 422
        )
    assert remote.requests == []  # Management never invokes a model or discovers implicitly.
