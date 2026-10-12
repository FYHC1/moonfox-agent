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
        "https://example.com/v1/responses",
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


@pytest.mark.parametrize(
    "identity", ["gpt-4.1", "GPT-4.1-mini", "模型-2.5", "qwen2.5:32b", "x" * 200]
)
def test_model_identifiers_preserve_names_and_are_not_provider_slugs(tmp_path, identity):
    store, values = catalog(tmp_path)
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    created = values.put_model(identity, model(), 0)
    assert created["id"] == identity and created["available"]
    settings = SessionSettings(model_id=identity)
    assert values.resolve(settings).model.id == "remote-a"
    values.put_model(identity, model().model_copy(update={"name": "renamed"}), 1)
    assert values.models()["models"][0]["id"] == identity
    values.delete_model(identity, 2)
    assert values.models()["models"] == []
    with pytest.raises(InvalidConfiguration):
        values.put_provider("gpt-4.1", provider(auth_mode="none"), expected_revision=0)
    store.close()


@pytest.mark.parametrize(
    "identity",
    [
        "",
        ".",
        "..",
        ".hidden",
        "name/child",
        "name\\child",
        "has space",
        "a?b",
        "a#b",
        "a%b",
        "bad\n",
        "a\x00b",
        "x" * 201,
    ],
)
def test_model_identifiers_reject_ambiguous_url_segments(tmp_path, identity):
    store, values = catalog(tmp_path)
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    with pytest.raises(InvalidConfiguration):
        values.put_model(identity, model(), 0)
    assert values.models()["models"] == []
    store.close()


def test_automatic_ids_prefer_name_then_provider_then_suffix_including_builtins(tmp_path):
    store, values = catalog(tmp_path)
    for identity in ("first", "second"):
        values.put_provider(identity, provider(auth_mode="none"), expected_revision=0)
    first = model().model_copy(update={"name": "gpt-4.1"})
    second = first.model_copy(update={"provider_id": "second"})
    assert values.put_model(None, first, 0)["id"] == "gpt-4.1"
    assert values.put_model(None, second, 0)["id"] == "second-gpt-4.1"
    assert values.put_model(None, second, 0)["id"] == "second-gpt-4.1-2"
    assert values.put_model(None, first, 0)["id"] == "first-gpt-4.1"
    assert (
        values.put_model(None, first.model_copy(update={"name": "demo"}), 0)["id"] == "first-demo"
    )
    with pytest.raises(InvalidConfiguration):
        values.put_model(None, first, 1)
    with pytest.raises(ConflictError):
        values.put_model("gpt-4.1", second, 0)
    assert store.providers.model("gpt-4.1").settings.provider_id == "first"
    store.close()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GPT-4.1", "GPT-4.1"),
        ("模型 A.2", "模型-A.2"),
        ("Qwen/Qwen2.5", "Qwen-Qwen2.5"),
        ("...", "model"),
        (".hidden", "hidden"),
        ("name?#%suffix", "name-suffix"),
    ],
)
def test_automatic_ids_sanitize_only_unsafe_path_characters(tmp_path, name, expected):
    store, values = catalog(tmp_path)
    values.put_provider("first", provider(auth_mode="none"), expected_revision=0)
    assert values.put_model(None, model().model_copy(update={"name": name}), 0)["id"] == expected
    store.close()


async def test_auto_id_default_and_session_survive_restart_and_display_name_edit(tmp_path):
    long_provider = "p" * 64
    long_name = "模型-4.1" + "x" * 94
    hub = create_application(home=tmp_path, models={}, secret_protector=TestProtector())
    await hub.start()
    await hub.put_provider(
        OWNER, long_provider, settings=provider(auth_mode="none"), expected_revision=0
    )
    settings = ModelSettings(provider_id=long_provider, name=long_name, remote_id="remote")
    first = await hub.put_model(OWNER, settings=settings, expected_revision=0)
    duplicate = await hub.put_model(OWNER, settings=settings, expected_revision=0)
    identity = duplicate["id"]
    assert first["id"] == long_name and identity == f"{long_provider}-{long_name}"
    assert len(identity) > 100
    revision = (await hub.managed_models(OWNER))["config_revision"]
    await hub.set_default_model(OWNER, identity, expected_revision=revision)
    session = await hub.create_session(OWNER, title="stable", request_id="session")
    await hub.put_model(
        OWNER,
        identity,
        settings=settings.model_copy(update={"name": "new name"}),
        expected_revision=1,
    )
    await hub.aclose()
    hub = create_application(home=tmp_path, models={}, secret_protector=TestProtector())
    await hub.start()
    try:
        assert (await hub.config(OWNER))["settings"]["defaults"]["model_id"] == identity
        snapshot = await hub.snapshot(OWNER, session["id"])
        assert snapshot["settings"]["model_id"] == identity
        rows = (await hub.managed_models(OWNER))["models"]
        assert next(row for row in rows if row["id"] == identity)["settings"]["name"] == "new name"
        with pytest.raises(ConflictError):
            await hub.delete_model(OWNER, identity, expected_revision=2)
    finally:
        await hub.aclose()


@pytest.mark.parametrize(
    "params",
    [
        GenerationParameters(stop=()),
        GenerationParameters(stop=("END",)),
        GenerationParameters(seed=0),
        GenerationParameters(frequency_penalty=0),
        GenerationParameters(presence_penalty=0),
    ],
)
def test_responses_rejects_chat_only_parameters_for_models_and_sessions(tmp_path, params):
    store, values = catalog(tmp_path)
    responses = provider(protocol="openai-responses", auth_mode="none")
    values.put_provider("first", responses, expected_revision=0)
    with pytest.raises(InvalidConfiguration):
        values.put_model("gpt-4.1", model(parameters=params), 0)
    values.put_model("gpt-4.1", model(supports_reasoning=True), 0)
    with pytest.raises(InvalidConfiguration):
        values.validate_session(SessionSettings(model_id="gpt-4.1", generation=params))
    settings = SessionSettings(model_id="gpt-4.1", thinking_level="high")
    assert values.resolve(settings).model.api == "openai-responses"
    assert values.resolve(settings).model.reasoning
    store.close()


async def test_responses_discovery_uses_bearer_and_protocol_switch_checks_existing_defaults(
    tmp_path,
):
    transport = Transport()
    store, values = catalog(tmp_path, transport=transport)
    values.put_provider(
        "first", provider(), expected_revision=0, secret_action="replace", api_key="synthetic-key"
    )
    values.put_model("gpt-4.1", model(parameters=GenerationParameters(stop=("END",))), 0)
    responses = provider(protocol="openai-responses")
    with pytest.raises(ConflictError):
        values.put_provider("first", responses, expected_revision=1)
    with pytest.raises(InvalidConfiguration):
        values.put_provider(
            "first", responses, expected_revision=1, secret_action="replace", api_key="new-key"
        )
    values.put_model("gpt-4.1", model(), 1)
    values.put_provider(
        "first", responses, expected_revision=1, secret_action="replace", api_key="new-key"
    )
    assert (await values.test_provider("first", 2))["ok"]
    request = transport.requests[0]
    assert request.method == "GET" and request.url.endswith("/models")
    assert request.headers["Authorization"] == "Bearer new-key" and request.payload is None
    assert values.resolve(SessionSettings(model_id="gpt-4.1")).model.api == "openai-responses"
    store.close()
