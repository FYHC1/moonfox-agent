"""Explicit plugin loading, authorization, first-request context and unload cleanup."""

import asyncio
import runpy
from pathlib import Path

from moonfox_agent.api import RunOptions
from moonfox_ai.api import FauxProvider, FauxResponse, Model, get_current_system_prompt
from moonfox_app.api import (
    ExtensionRegistry,
    InMemorySessionStore,
    SessionBinding,
    create_headless_app,
)


async def main() -> None:
    # Explicitly execute a known local plugin. Never scan arbitrary directories.
    plugin = runpy.run_path(str(Path(__file__).with_name("plugin.py")))
    registry = ExtensionRegistry()
    manifest = plugin["MANIFEST"]
    registry.register(manifest, plugin["GreetingExtension"])
    faux = FauxProvider([FauxResponse.text("Hello from an extension-enabled session.")])
    async with await create_headless_app(
        store=InMemorySessionStore(),
        binding=SessionBinding("demo", "demo-scope"),
        model=Model("offline", "faux", "faux"),
        stream_fn=faux.stream,
        registry=registry,
        enabled_extensions=("greeting",),
        grants={"greeting": frozenset(manifest.capabilities)},
    ) as app:
        result = await app.prompt("Hello", options=RunOptions(2))
        assert "friendly" in get_current_system_prompt(faux.requests[0][1].messages)
        assert "demonstration" in str(faux.requests[0][1].messages)
        print(f"extension={app.active_extensions[0]}; outcome={result.reason}")
        await app.disable_extension("greeting")
        assert "friendly" not in get_current_system_prompt(app.view().messages)
        print(f"unloaded={len(app.active_extensions) == 0}; prompt_cleared=True")


if __name__ == "__main__":
    asyncio.run(main())
