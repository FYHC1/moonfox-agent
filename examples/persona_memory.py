"""Optional business extensions, with no Web/QQ transport or external model calls."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from moonfox_ai.api import FauxProvider, FauxResponse, Model, get_current_system_prompt
from moonfox_app.api import (
    ChannelBinding,
    HostSettings,
    ModelRegistration,
    Principal,
    SessionSettings,
    create_application,
)
from moonfox_ext_memory import EXTENSION as MEMORY
from moonfox_ext_persona import EXTENSION as PERSONA


async def main(home: Path) -> None:
    faux = FauxProvider([FauxResponse.text("offline reply"), FauxResponse.text("plain reply")])
    admin = Principal("demo-owner", True)
    hub = create_application(
        home=home,
        models={"demo": ModelRegistration(Model("demo", "faux", "faux"), faux.stream, "Offline")},
        session_extensions=(PERSONA, MEMORY),
        defaults=HostSettings(defaults=SessionSettings(enabled_extensions=("persona", "memory"))),
    )
    await hub.start()
    try:
        identity = (await hub.create_session(admin, title="Extension demo", request_id="new"))["id"]
        await hub.extension_command(
            admin,
            identity,
            "memory",
            command="remember",
            arguments_json=json.dumps({"text": "偏好无糖冰美式", "kind": "preference"}),
            expected_revision=1,
            request_id="remember",
        )
        # This is an authorized channel routing seam, not a real QQ connection.
        channel = ChannelBinding("offline", "test-bot", admin.id, frozenset({"private:peer"}))
        qq = hub.channel_session(channel, "private:peer")
        isolated = await hub.extension_command(
            admin,
            qq.id,
            "memory",
            command="list",
            arguments_json="{}",
            expected_revision=1,
            request_id="list-qq",
        )
        await hub.submit_input(
            admin, identity, text="冰美式", request_id="first", idempotency_key="first"
        )
        await hub.wait_for_idle(identity)
        request = faux.requests[0][1].messages
        persona = "人设：月狐" in get_current_system_prompt(request)
        recall = any("偏好无糖冰美式" in str(message.content) for message in request)
        qq_isolated = json.loads(isolated["result_json"])["records"] == []
        print(f"persona={persona}; recall={recall}; qq_isolated={qq_isolated}")
        await hub.update_session_config(
            admin, identity, settings=SessionSettings(), expected_revision=1
        )
        await hub.wait_for_idle(identity)
        await hub.submit_input(
            admin, identity, text="plain", request_id="second", idempotency_key="second"
        )
        await hub.wait_for_idle(identity)
        plain = "人设：月狐" not in get_current_system_prompt(faux.requests[-1][1].messages)
        disabled = not (await hub.snapshot(admin, identity))["settings"]["enabled_extensions"]
        print(f"disabled={disabled}; plain_agent={plain}")
    finally:
        await hub.aclose()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="moonfox-business-") as directory:
        asyncio.run(main(Path(directory)))
