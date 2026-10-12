"""Offline save/reopen/continue example. Installs only AI, Agent, and moonfox-app."""

import asyncio
import tempfile
from pathlib import Path

from moonfox_agent.api import RunOptions
from moonfox_ai.api import FauxProvider, FauxResponse, Model
from moonfox_app.api import JsonlSessionStore, SessionBinding, create_headless_app


async def main() -> None:
    binding = SessionBinding("example-session", "example-private-scope")
    model = Model("offline", "faux", "faux")
    with tempfile.TemporaryDirectory(prefix="moonfox-headless-") as directory:
        path = Path(directory) / "session.jsonl"
        first = FauxProvider([FauxResponse.text("Saved without a server or extensions.")])
        async with await create_headless_app(
            store=JsonlSessionStore(path, create=True),
            binding=binding,
            model=model,
            stream_fn=first.stream,
            system_prompt="Be helpful.",
        ) as app:
            outcome = await app.prompt("Remember this conversation.", options=RunOptions(2))
            saved = app.view()
            print(f"saved={outcome.reason}; messages={len(saved.messages)}")

        second = FauxProvider([FauxResponse.text("Restored conversation is available.")])
        async with await create_headless_app(
            store=JsonlSessionStore(path),
            binding=binding,
            stream_fn=second.stream,
        ) as restored:
            assert restored.view() == saved
            outcome = await restored.prompt("What happened?", options=RunOptions(2))
            print(
                f"restored={outcome.reason}; messages={len(restored.view().messages)}; "
                f"extensions={len(restored.active_extensions)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
