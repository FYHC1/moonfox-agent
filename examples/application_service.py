"""Durable input receipt and background execution with no web dependencies."""

import asyncio
import tempfile
from pathlib import Path

from moonfox_ai.api import FauxProvider, FauxResponse, Model
from moonfox_app.api import ModelRegistration, Principal, create_application


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="moonfox-host-") as directory:
        provider = FauxProvider([FauxResponse.text("Saved by the background session actor.")])
        hub = create_application(
            home=Path(directory),
            models={
                "demo": ModelRegistration(
                    Model("demo", "faux", "faux"), provider.stream, "Offline test"
                ),
            },
        )
        owner = Principal("example-owner", True)
        await hub.start()
        try:
            session = await hub.create_session(owner, title="Headless service", request_id="create")
            receipt = await hub.submit_input(
                owner, session["id"], text="hello", request_id="input", idempotency_key="input"
            )
            await hub.wait_for_idle()
            snapshot = await hub.snapshot(owner, session["id"])
            print(
                f"accepted={receipt['status']}; settled={snapshot['inputs'][-1]['status']}; "
                f"messages={len(snapshot['messages'])}"
            )
        finally:
            await hub.aclose()


if __name__ == "__main__":
    asyncio.run(main())
