"""Example plugin: imports only the public SDK, not runtime or extension internals."""

from moonfox_app.extension_api import (
    ContextBlock,
    CustomEntryDraft,
    ExtensionManifest,
    PromptSection,
    SessionExtensionAPI,
    SessionExtensionContext,
    SessionView,
)

MANIFEST = ExtensionManifest(
    id="greeting",
    version="0.1.0",
    sdk_version_range=">=0.1,<0.2",
    scope="session",
    capabilities=("prompt", "context", "entries"),
)


class GreetingExtension:
    async def setup(self, api: SessionExtensionAPI) -> None:
        api.register_prompt_section(self.sections)
        api.register_context_provider(self.references)

    async def attach(self, ctx: SessionExtensionContext) -> None:
        ctx.actions.stage_entries([CustomEntryDraft("greeting.attached", {"version": "0.1.0"})])

    async def sections(self, view: SessionView) -> tuple[PromptSection, ...]:
        return (PromptSection("greeting.style", "Use short, friendly replies."),)

    async def references(self, view: SessionView) -> tuple[ContextBlock, ...]:
        return (ContextBlock("greeting.example", "This is a headless extension demonstration."),)

    async def detach(self) -> None:
        pass

    async def dispose(self) -> None:
        pass
