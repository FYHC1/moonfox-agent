import asyncio
import json
import sqlite3
import sys

import pytest
from pydantic import ValidationError

from moonfox_ai.api import FauxProvider, Model
from moonfox_app.api import Principal, SessionSettings, create_application
from moonfox_app.config.providers import (
    GenerationParameters,
    ModelSettings,
    ProviderSettings,
    merge_parameters,
    validate_parameters,
)
from moonfox_app.security import AccessDenied
from moonfox_app.security.secrets import EnvironmentOnly, SecretStorageError, WindowsDPAPI
from moonfox_app.services.errors import ConflictError, InvalidConfiguration
from moonfox_app.services.model_catalog import ModelCatalog
from moonfox_app.services.store import HostStore
from moonfox_app.services.types import ModelRegistration

OWNER = Principal("owner", True)
OTHER = Principal("reader")


class TestProtector:
    """Test fake: not cryptography, no production fallback uses this implementation."""

    kind = "custom"

    def protect(self, value, *, context):
        return (context + "|" + value[::-1]).encode()

    def unprotect(self, value, *, context):
        prefix, text = value.decode().split("|", 1)
        if prefix != context:
            raise SecretStorageError("wrong binding")
        return text[::-1]


class Response:
    def __init__(self, payload=None, status=200):
        self.status = status
        self.headers = {"content-type": "application/json"}
        self.payload = (
            {"data": [{"id": "remote-a", "name": "模型 A"}]} if payload is None else payload
        )
        self.closed = False

    async def iter_lines(self):
        yield json.dumps(self.payload)

    async def aclose(self):
        self.closed = True


class Transport:
    def __init__(self, response=None):
        self.response = response or Response()
        self.requests = []

    async def __call__(self, request, signal):
        self.requests.append(request)
        return self.response


def catalog(tmp_path, **kwargs):
    store = HostStore(tmp_path)
    builtin = {
        "demo": ModelRegistration(Model("demo", "faux", "faux"), FauxProvider([]).stream, "Demo")
    }
    value = ModelCatalog(
        store, builtin, protector=kwargs.pop("protector", TestProtector()), **kwargs
    )
    return store, value


def provider(**kwargs):
    return ProviderSettings(name="测试服务", base_url="https://example.test/v1", **kwargs)


def model(**kwargs):
    return ModelSettings(provider_id="first", name="测试模型", remote_id="remote-a", **kwargs)


def test_provider_keys_are_write_only_and_persist_with_cas(tmp_path):
    store, values = catalog(tmp_path)
    key = "synthetic-only-secret-123"
    created = values.put_provider(
        "first", provider(), expected_revision=0, secret_action="replace", api_key=key
    )
    assert created["key_configured"] and created["revision"] == 1
    assert key not in str(created) + str(values.providers()) + repr(
        store.providers.provider("first")
    )
    stored = store.providers.provider("first")
    assert key.encode() not in stored.secret
    changed = values.put_provider("first", provider(enabled=False), expected_revision=1)
    assert changed["key_configured"] and changed["revision"] == 2
    with pytest.raises(ConflictError):
        values.put_provider("first", provider(), expected_revision=1, secret_action="clear")
    assert values.providers()["providers"][0]["key_configured"]
    store.close()
    store, values = catalog(tmp_path)
    assert values.providers()["providers"][0]["revision"] == 2
    cleared = values.put_provider("first", provider(), expected_revision=2, secret_action="clear")
    assert not cleared["key_configured"]
    assert store.providers.provider("first").secret is None
    store.close()
    assert key.encode() not in (tmp_path / "host.sqlite").read_bytes()


def test_destination_change_never_reuses_stored_key_implicitly(tmp_path):
    store, values = catalog(tmp_path)
    values.put_provider(
        "first", provider(), expected_revision=0, secret_action="replace", api_key="synthetic-key"
    )
    changed = provider().model_copy(update={"base_url": "https://other.test/v1"})
    with pytest.raises(ConflictError):
        values.put_provider("first", changed, expected_revision=1)
    accepted = values.put_provider(
        "first", changed, expected_revision=1, secret_action="replace", api_key="synthetic-new-key"
    )
    assert accepted["revision"] == 2
    store.close()


def test_multiple_providers_model_references_enable_and_delete(tmp_path):
    store, values = catalog(tmp_path)
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    values.put_provider(
        "second", provider(auth_mode="none", protocol="anthropic"), expected_revision=0
    )
    created = values.put_model("local-a", model(), 0)
    assert created["available"]
    values.put_model(
        "local-b", model().model_copy(update={"provider_id": "second", "name": "模型 B"}), 0
    )
    assert len(values.providers()["providers"]) == 2 and len(values.models()["models"]) == 2
    with pytest.raises(ConflictError):
        values.delete_provider("first", 1)
    with pytest.raises(ConflictError):
        values.put_model("demo", model(), 0)
    with pytest.raises(ConflictError):
        values.put_model("local-a", model(), 0)
    values.put_provider("first", provider(auth_mode="none", enabled=False), expected_revision=1)
    assert not values.model_view(store.providers.model("local-a"))["available"]
    assert values.model_descriptor("local-a").id == "remote-a"
    assert {item["id"] for item in values.choices()} == {"demo", "local-b"}
    with pytest.raises(InvalidConfiguration):
        values.resolve(SessionSettings(model_id="local-a"))
    values.delete_model("local-a", 1)
    values.delete_provider("first", 2)
    assert len(values.providers()["providers"]) == 1
    store.close()


def test_session_and_default_references_block_model_deletion(tmp_path):
    store, values = catalog(tmp_path)
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    values.put_model("local-a", model(), 0)
    settings = SessionSettings(model_id="local-a")
    store.create_session(
        owner_id="owner", domain="qq", title="channel", request_id="r", settings=settings
    )
    with pytest.raises(ConflictError):
        values.delete_model("local-a", 1)
    store.close()


def test_environment_mode_never_copies_key_to_database(tmp_path):
    environment = {"TEST_KEY": "synth-env-secret"}
    store, values = catalog(tmp_path, environment=environment, protector=EnvironmentOnly())
    values.put_provider("first", provider(auth_mode="env", env_var="TEST_KEY"), expected_revision=0)
    assert values.providers()["providers"][0]["key_configured"]
    assert store.providers.provider("first").secret is None
    values.put_model("local-a", model(), 0)
    assert values.models()["models"][0]["available"]
    environment.clear()
    assert not values.models()["models"][0]["available"]
    with pytest.raises(InvalidConfiguration):
        values.put_provider(
            "second", provider(), expected_revision=0, secret_action="replace", api_key="test"
        )
    store.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:key@example.com/v1",
        "https://example.com/v1?key=x",
        "https://example.com/v1#x",
        "file:///secret",
        "https://example.com/v1/chat/completions",
        "https://example.com/v1/../models",
        "https://example.com/\npath",
    ],
)
def test_endpoint_validation_rejects_unsafe_or_wrong_base_urls(url):
    with pytest.raises(ValidationError):
        ProviderSettings(name="invalid", base_url=url)


@pytest.mark.parametrize(
    "params",
    [
        {"temperature": float("nan")},
        {"top_p": 0},
        {"max_tokens": True},
        {"temperature": 2.1},
        {"seed": -1},
        {"stop": ("",)},
        {"stop": ("a", "b", "c", "d", "e")},
    ],
)
def test_generation_parameters_are_strict_bounded(params):
    with pytest.raises(ValidationError):
        GenerationParameters(**params)


def test_generation_merge_and_protocol_capabilities():
    defaults = GenerationParameters(temperature=0.2, stop=("end",), seed=4)
    merged = merge_parameters(defaults, GenerationParameters(temperature=0.0, stop=()))
    assert merged.temperature == 0.0 and merged.stop == () and merged.seed == 4
    anthropic = provider(protocol="anthropic", auth_mode="none")
    with pytest.raises(ValueError):
        validate_parameters(model(parameters=defaults), anthropic)
    with pytest.raises(ValueError):
        validate_parameters(model(), anthropic, GenerationParameters(temperature=0.5, top_p=0.9))
    with pytest.raises(ValueError):
        validate_parameters(model(), provider(), thinking_level="high")
    with pytest.raises(ValueError):
        validate_parameters(model(), provider(), GenerationParameters(max_tokens=8192))


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (Response(status=401), "auth_error"),
        (Response(status=404), "unsupported"),
        (Response(status=503), "unavailable"),
        (Response(payload={"wrong": []}), "invalid_response"),
        (Response(), "ok"),
    ],
)
async def test_provider_probe_uses_only_models_endpoint_and_closes(tmp_path, response, status):
    transport = Transport(response)
    store, values = catalog(tmp_path, transport=transport)
    values.put_provider(
        "first", provider(), expected_revision=0, secret_action="replace", api_key="synthetic-key"
    )
    result = await values.test_provider("first", 1)
    assert result["status"] == status and response.closed
    request = transport.requests[0]
    assert (
        request.method == "GET" and request.url.endswith("/v1/models") and request.payload is None
    )
    assert "synthetic-key" not in repr(request) + str(result)
    with pytest.raises(ConflictError):
        await values.test_provider("first", 0)
    store.close()


async def test_admin_only_empty_host_and_model_default(tmp_path):
    hub = create_application(home=tmp_path, models={}, secret_protector=TestProtector())
    await hub.start()
    assert (await hub.managed_models(OWNER))["models"] == []
    with pytest.raises(AccessDenied):
        await hub.providers(OTHER)
    with pytest.raises(AccessDenied):
        await hub.put_provider(OTHER, "first", settings=provider(), expected_revision=0)
    with pytest.raises(InvalidConfiguration):
        await hub.create_session(OWNER, title="not ready", request_id="create")
    await hub.put_provider(OWNER, "first", settings=provider(auth_mode="none"), expected_revision=0)
    await hub.put_model(OWNER, "local-a", settings=model(), expected_revision=0)
    catalog_view = await hub.managed_models(OWNER)
    await hub.set_default_model(OWNER, "local-a", expected_revision=catalog_view["config_revision"])
    session = await hub.create_session(OWNER, title="ready", request_id="create")
    assert session["model_id"] == "local-a"
    await hub.aclose()
    hub = create_application(home=tmp_path, models={}, secret_protector=TestProtector())
    await hub.start()
    assert (await hub.config(OWNER))["settings"]["defaults"]["model_id"] == "local-a"
    assert (await hub.list_sessions(OWNER))[0]["id"] == session["id"]
    await hub.aclose()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_real_dpapi_binds_ciphertext_to_provider_context():
    protector = WindowsDPAPI()
    blob = protector.protect("synthetic-not-a-real-key", context="first")
    assert b"synthetic-not-a-real-key" not in blob
    assert protector.unprotect(blob, context="first") == "synthetic-not-a-real-key"
    with pytest.raises(SecretStorageError):
        protector.unprotect(blob, context="second")


def test_v1_host_database_migrates_without_changing_sessions(tmp_path):
    store = HostStore(tmp_path)
    record = store.create_session(
        owner_id="x", domain="agent", title="old", request_id="r", settings=SessionSettings()
    )
    store.close()
    with sqlite3.connect(tmp_path / "host.sqlite") as db:
        db.execute("DROP TABLE models")
        db.execute("DROP TABLE providers")
        db.execute("PRAGMA user_version=1")
    store = HostStore(tmp_path)
    assert store.session(record.id) == record
    assert store.providers.providers() == []
    store.close()


async def test_probe_filters_invalid_ids_and_never_truncates_model_identity(tmp_path):
    response = Response(
        {
            "data": [
                {"id": "a" * 201},
                {"id": "bad\nname"},
                {"id": "good", "display_name": "valid\nname"},
            ]
        }
    )
    store, values = catalog(tmp_path, transport=Transport(response))
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    result = await values.test_provider("first", 1)
    assert result["models"] == [{"id": "good", "name": "validname"}]
    assert response.closed
    store.close()


async def test_probe_credential_echo_discards_partial_list(tmp_path):
    key = "synthetic-only-secret"
    response = Response({"data": [{"id": "valid"}, {"id": "bad", "name": key}]})
    store, values = catalog(tmp_path, transport=Transport(response))
    values.put_provider(
        "first", provider(), expected_revision=0, secret_action="replace", api_key=key
    )
    result = await values.test_provider("first", 1)
    assert result["status"] == "invalid_response" and result["models"] == []
    assert key not in str(result) and response.closed
    store.close()


async def test_probe_repeated_cancellation_waits_for_response_cleanup(tmp_path):
    reading, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowResponse(Response):
        async def iter_lines(self):
            reading.set()
            await asyncio.Event().wait()
            yield ""

        async def aclose(self):
            closing.set()
            await release.wait()
            self.closed = True

    response = SlowResponse()
    store, values = catalog(tmp_path, transport=Transport(response))
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    task = asyncio.create_task(values.test_provider("first", 1))
    await asyncio.wait_for(reading.wait(), 2)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not response.closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.closed
    store.close()
