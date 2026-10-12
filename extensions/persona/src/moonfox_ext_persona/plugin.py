"""Persona lifecycle, using only the public extension SDK."""

import json

from moonfox_app.extension_api import (
    CustomEntryDraft,
    ExtensionManifest,
    ExtensionResources,
    PromptSection,
    SessionExtensionAPI,
    SessionExtensionContext,
    SessionExtensionDefinition,
    SessionView,
)

from .render import render_persona
from .schema import PersonaSettings, validate_config

MANIFEST = ExtensionManifest(
    id="persona",
    version="0.1.0",
    sdk_version_range=">=0.1,<0.2",
    scope="session",
    capabilities=("prompt", "entries"),
)


class PersonaExtension:
    def __init__(self, resources: ExtensionResources, config_json: str = "{}") -> None:
        self.settings = PersonaSettings.model_validate_json(config_json)
        self._binding = resources.binding
        self._text = render_persona(self.settings)
        self._active = False

    async def setup(self, api: SessionExtensionAPI) -> None:
        api.register_prompt_section(self.sections, required=True)

    async def attach(self, ctx: SessionExtensionContext) -> None:
        if ctx.binding != self._binding:
            raise ValueError("Persona binding does not match the authorized session")
        ctx.actions.stage_entries(
            [
                CustomEntryDraft(
                    "persona.profile",
                    {
                        "profile_id": self.settings.profile_id,
                        "revision": self.settings.revision,
                        "content_hash": self.settings.content_hash,
                        "extension_version": MANIFEST.version,
                    },
                )
            ]
        )
        self._active = True

    async def sections(self, view: SessionView) -> tuple[PromptSection, ...]:
        if not self._active or view.binding != self._binding:
            raise RuntimeError("Persona capability is inactive or incorrectly bound")
        return (PromptSection("persona.identity", self._text),)

    async def detach(self) -> None:
        self._active = False

    async def dispose(self) -> None:
        self._active = False


EXTENSION = SessionExtensionDefinition(
    manifest=MANIFEST,
    factory=PersonaExtension,
    validate_config=validate_config,
    config_schema_json=json.dumps(PersonaSettings.model_json_schema(), ensure_ascii=False),
    default_config_json=PersonaSettings().model_dump_json(),
)
