"""Persistent provider/model catalog shared by browser, headless and channel sessions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from moonfox_ai.api import (
    AnthropicProvider,
    HTTPRequest,
    HTTPResponse,
    HTTPTransport,
    Model,
    OpenAIProvider,
    StreamOptions,
)
from moonfox_app.config import SessionSettings
from moonfox_app.config.providers import (
    IDENTIFIER,
    ModelSettings,
    ProviderSettings,
    validate_parameters,
)
from moonfox_app.security.secrets import SecretProtector, SecretStorageError, default_protector

from .errors import ConflictError, InvalidConfiguration, NotFoundError
from .http_transport import HttpxTransport, close_response
from .provider_store import ModelRecord, ProviderRecord
from .store import HostStore
from .types import ModelRegistration


class ModelCatalog:
    def __init__(
        self,
        store: HostStore,
        builtin: Mapping[str, ModelRegistration],
        *,
        transport: HTTPTransport | None = None,
        protector: SecretProtector | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._rows = store.providers
        self._builtin = dict(builtin)
        self._transport = transport or HttpxTransport()
        self._protector = protector or default_protector()
        self._environment = os.environ if environment is None else environment
        self._testing: set[str] = set()
        if any(row.id in self._builtin for row in self._rows.models()):
            raise InvalidConfiguration("Managed model ID conflicts with a builtin model")

    @staticmethod
    def _identity(identity: str) -> None:
        if not re.fullmatch(IDENTIFIER, identity):
            raise InvalidConfiguration("Invalid provider/model identifier")

    @staticmethod
    def _revision(value: int) -> None:
        if type(value) is not int or value < 0:
            raise InvalidConfiguration("Expected revision must be a nonnegative integer")

    def _credential(self, provider: ProviderRecord) -> str | None:
        settings = provider.settings
        if settings.auth_mode == "none":
            return None
        if settings.auth_mode == "env":
            value = self._environment.get(settings.env_var or "")
            if not value:
                raise InvalidConfiguration("Provider credential is not configured")
        else:
            if provider.secret is None or provider.secret_format != self._protector.kind:
                raise InvalidConfiguration("Provider credential is unavailable on this host")
            try:
                value = self._protector.unprotect(provider.secret, context=provider.id)
            except SecretStorageError:
                raise InvalidConfiguration("Provider credential cannot be unlocked") from None
        if (
            not value.strip()
            or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise InvalidConfiguration("Provider credential is invalid")
        return value

    def provider_view(self, provider: ProviderRecord) -> dict[str, Any]:
        configured = False
        try:
            configured = self._credential(provider) is not None
        except InvalidConfiguration:
            pass
        source = {"stored": "stored", "env": "environment", "none": "none"}[
            provider.settings.auth_mode
        ]
        return {
            "id": provider.id,
            "revision": provider.revision,
            "settings": provider.settings.model_dump(mode="json"),
            "key_configured": configured,
            "credential_source": source,
        }

    def providers(self) -> dict[str, Any]:
        kind = self._protector.kind if self._protector.kind in ("dpapi", "env_only") else "custom"
        return {
            "providers": [self.provider_view(row) for row in self._rows.providers()],
            "secret_storage": kind,
        }

    def put_provider(
        self,
        identity: str,
        settings: ProviderSettings,
        *,
        expected_revision: int,
        secret_action: str = "keep",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        self._identity(identity)
        self._revision(expected_revision)
        try:
            settings = ProviderSettings.model_validate_json(settings.model_dump_json())
        except ValidationError:
            raise InvalidConfiguration("Invalid provider settings") from None
        if secret_action not in ("keep", "replace", "clear"):
            raise InvalidConfiguration("Invalid credential action")
        if secret_action == "replace":
            if (
                settings.auth_mode != "stored"
                or not api_key
                or not api_key.strip()
                or len(api_key) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in api_key)
            ):
                raise InvalidConfiguration("A valid stored credential is required")
        elif api_key is not None:
            raise InvalidConfiguration("Credential must be omitted for keep/clear")
        try:
            old = self._rows.provider(identity)
        except NotFoundError:
            old = None
        if (old.revision if old else 0) != expected_revision:
            raise ConflictError("Provider revision changed")
        if (
            old
            and old.secret is not None
            and secret_action == "keep"
            and settings.auth_mode == "stored"
        ):
            if (old.settings.base_url, old.settings.protocol) != (
                settings.base_url,
                settings.protocol,
            ):
                raise ConflictError("Changing destination requires an explicit credential update")
        secret, secret_format = (old.secret, old.secret_format) if old else (None, None)
        if settings.auth_mode != "stored" or secret_action == "clear":
            secret, secret_format = None, None
        if secret_action == "replace":
            assert api_key is not None
            try:
                secret = self._protector.protect(api_key, context=identity)
                secret_format = self._protector.kind
            except SecretStorageError:
                raise InvalidConfiguration(
                    "Stored credentials unavailable; use environment references"
                ) from None
        # Changing protocol cannot strand already configured incompatible model parameters.
        try:
            for model in self._rows.models():
                if model.settings.provider_id == identity:
                    validate_parameters(model.settings, settings)
        except ValueError:
            raise InvalidConfiguration(
                "Existing model settings are incompatible with this provider"
            ) from None
        record = self._rows.put_provider(
            identity, settings, expected_revision, secret, secret_format
        )
        return self.provider_view(record)

    def delete_provider(self, identity: str, revision: int) -> None:
        self._identity(identity)
        self._revision(revision)
        self._rows.delete_provider(identity, revision)

    def _unavailable(self, model: ModelRecord) -> str | None:
        if not model.settings.enabled:
            return "模型已停用"
        provider = self._rows.provider(model.settings.provider_id)
        if not provider.settings.enabled:
            return "提供商已停用"
        try:
            self._credential(provider)
            validate_parameters(model.settings, provider.settings)
        except (InvalidConfiguration, ValueError):
            return "凭据不可用或模型参数不兼容"
        return None

    def model_view(self, record: ModelRecord) -> dict[str, Any]:
        reason = self._unavailable(record)
        return {
            "id": record.id,
            "revision": record.revision,
            "settings": record.settings.model_dump(mode="json"),
            "available": reason is None,
            "unavailable_reason": reason,
        }

    def models(self) -> dict[str, Any]:
        revision, config = self._store.config()
        return {
            "models": [self.model_view(row) for row in self._rows.models()],
            "builtin_models": [
                {"id": identity, "label": item.label, "provider": item.model.provider}
                for identity, item in self._builtin.items()
            ],
            "default_model_id": config.defaults.model_id,
            "config_revision": revision,
        }

    def choices(self) -> list[dict[str, str]]:
        result = [
            {"id": identity, "label": item.label, "provider": item.model.provider}
            for identity, item in self._builtin.items()
        ]
        result.extend(
            {"id": row.id, "label": row.settings.name, "provider": row.settings.provider_id}
            for row in self._rows.models()
            if self._unavailable(row) is None
        )
        return result

    def put_model(self, identity: str, settings: ModelSettings, revision: int) -> dict[str, Any]:
        self._identity(identity)
        self._revision(revision)
        if identity in self._builtin:
            raise ConflictError("Builtin models cannot be edited")
        try:
            settings = ModelSettings.model_validate_json(settings.model_dump_json())
            provider = self._rows.provider(settings.provider_id)
            validate_parameters(settings, provider.settings)
        except ValueError:
            raise InvalidConfiguration(
                "Model settings are incompatible or outside limits"
            ) from None
        return self.model_view(self._rows.put_model(identity, settings, revision))

    def delete_model(self, identity: str, revision: int) -> None:
        self._identity(identity)
        self._revision(revision)
        if identity in self._builtin:
            raise ConflictError("Builtin models cannot be deleted")
        if self._store.config()[1].defaults.model_id == identity or any(
            session.settings.model_id == identity for session in self._store.sessions()
        ):
            raise ConflictError("Model is referenced by a default or session configuration")
        self._rows.delete_model(identity, revision)

    def model_descriptor(self, identity: str) -> Model:
        """Descriptor remains readable for disabled/no-key models, preserving history."""
        if identity in self._builtin:
            return self._builtin[identity].model
        model = self._rows.model(identity).settings
        provider = self._rows.provider(model.provider_id).settings
        return Model(
            id=model.remote_id,
            provider=model.provider_id,
            api="openai-chat" if provider.protocol == "openai" else "anthropic-messages",
            name=model.name,
            base_url=provider.base_url,
            context_window=model.context_window,
            max_tokens=model.max_output_tokens,
            reasoning=model.supports_reasoning,
            input=("text", "image") if model.supports_images else ("text",),
        )

    def validate_session(self, settings: SessionSettings, *, available: bool = True) -> None:
        if not available:
            return
        if settings.model_id in self._builtin:
            return
        try:
            model = self._rows.model(settings.model_id)
            provider = self._rows.provider(model.settings.provider_id)
            validate_parameters(
                model.settings, provider.settings, settings.generation, settings.thinking_level
            )
            if available:
                if self._unavailable(model) is not None:
                    raise InvalidConfiguration(
                        "Selected model is disabled or credentials are unavailable"
                    )
        except NotFoundError:
            raise InvalidConfiguration("Model is not registered") from None
        except ValueError:
            raise InvalidConfiguration(
                "Generation parameters are incompatible with this model"
            ) from None

    def resolve(self, settings: SessionSettings) -> ModelRegistration:
        """Freeze model, key, URL, generation parameters per run, including after hot edits."""
        self.validate_session(settings)
        if settings.model_id in self._builtin:
            builtin = self._builtin[settings.model_id]
            options = replace(builtin.options, **settings.generation.model_dump(exclude_none=True))
            return replace(builtin, options=options)
        row = self._rows.model(settings.model_id)
        provider = self._rows.provider(row.settings.provider_id)
        params = validate_parameters(
            row.settings, provider.settings, settings.generation, settings.thinking_level
        )
        key = self._credential(provider)
        if provider.settings.protocol == "openai":
            adapter = OpenAIProvider(
                self._transport,
                base_url=provider.settings.base_url,
                api_key=key,
                timeout_seconds=float(provider.settings.timeout_seconds),
                token_field=row.settings.token_field,
                supports_tools=row.settings.supports_tools,
                supports_images=row.settings.supports_images,
                supports_reasoning=row.settings.supports_reasoning,
            )
            stream = adapter.stream
        else:
            stream = AnthropicProvider(
                self._transport,
                base_url=provider.settings.base_url,
                api_key=key,
                timeout_seconds=float(provider.settings.timeout_seconds),
                supports_tools=row.settings.supports_tools,
                supports_images=row.settings.supports_images,
            ).stream
        options = StreamOptions(**params.model_dump())
        return ModelRegistration(
            self.model_descriptor(settings.model_id),
            stream,
            row.settings.name,
            options=options,
            revision=f"{row.revision}:{provider.revision}",
            supports_tools=row.settings.supports_tools,
        )

    async def test_provider(self, identity: str, revision: int) -> dict[str, Any]:
        self._identity(identity)
        self._revision(revision)
        provider = self._rows.provider(identity)
        if provider.revision != revision:
            raise ConflictError("Provider revision changed")
        if identity in self._testing or len(self._testing) >= 4:
            raise ConflictError("Provider check already running")
        self._testing.add(identity)
        started = time.monotonic()
        response: HTTPResponse | None = None
        status, detail, models = "unavailable", "连接失败，请核对地址、凭据与服务状态。", []
        try:
            key = self._credential(provider)
            headers = {"Accept": "application/json"}
            if provider.settings.protocol == "openai" and key:
                headers["Authorization"] = f"Bearer {key}"
            elif provider.settings.protocol == "anthropic":
                headers["anthropic-version"] = "2023-06-01"
                if key:
                    headers["x-api-key"] = key
            async with asyncio.timeout(min(provider.settings.timeout_seconds, 30)):
                response = await self._transport(
                    HTTPRequest(
                        "GET",
                        provider.settings.base_url + "/models",
                        headers,
                        timeout_seconds=min(provider.settings.timeout_seconds, 30),
                    ),
                    None,
                )
                if response.status in (401, 403):
                    status, detail = "auth_error", "认证失败，请更新凭据或检查模型权限。"
                elif response.status in (404, 405):
                    status, detail = "unsupported", "该服务不支持模型列表，请手动填写远端模型 ID。"
                elif response.status == 200:
                    lines: list[str] = []
                    total = 0
                    async for line in response.iter_lines():
                        total += len(line.encode("utf-8")) + 1
                        if total > 1024 * 1024:
                            raise ValueError("List too large")
                        lines.append(line)
                    data = json.loads("\n".join(lines))
                    entries = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(entries, list):
                        raise ValueError("Invalid model list")
                    for entry in entries[:200]:
                        if (
                            isinstance(entry, dict)
                            and isinstance(entry.get("id"), str)
                            and entry["id"]
                        ):
                            remote_id = entry["id"]
                            name = entry.get("display_name", entry.get("name", remote_id))
                            if key and (key in remote_id or key in str(name)):
                                raise ValueError("Response contains credential")
                            if (
                                len(remote_id) > 200
                                or remote_id != remote_id.strip()
                                or any(ord(c) < 32 or ord(c) == 127 for c in remote_id)
                            ):
                                continue
                            name = name if isinstance(name, str) and name.strip() else remote_id
                            name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:100]
                            models.append({"id": remote_id, "name": name or remote_id[:100]})
                    status, detail = "ok", "连接成功，已读取模型列表；未发送聊天或生成请求。"
        except InvalidConfiguration:
            status, detail = "auth_error", "未配置可用凭据。"
        except (ValueError, TypeError, RecursionError):
            status, detail = "invalid_response", "服务未返回有效的模型列表。"
        except (OSError, TimeoutError):
            pass
        finally:
            try:
                if response is not None:
                    await close_response(response)
            except OSError:
                status, detail = "unavailable", "连接关闭失败，请检查服务状态。"
            finally:
                self._testing.discard(identity)
        if status != "ok":
            models.clear()
        return {
            "ok": status == "ok",
            "status": status,
            "detail": detail,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "models": models,
        }
