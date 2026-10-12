"""Deterministic, web-dependency-free exports from the protocol DTO source."""

from typing import Any

from moonfox_app.config import SessionSettings

from .schema import (
    PROTOCOL_VERSION,
    SCHEMAS,
    InputView,
    MessageView,
    SessionSummary,
    Snapshot,
    WireEvent,
)


def protocol_document() -> dict[str, Any]:
    """Each entry is a standalone JSON Schema document consumable by AJV2020."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "schemas": {
            name: {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                **model.model_json_schema(),
            }
            for name, model in sorted(SCHEMAS.items())
        },
    }


def protocol_fixtures() -> dict[str, Any]:
    """A snapshot and its contiguous replay, with a terminal fresh-cursor resync."""
    session = SessionSummary(
        id="session-demo",
        title="中文演示对话",
        domain="agent",
        owner_id="demo-user",
        created_at=1700000000000,
        updated_at=1700000000000,
        status="idle",
        pending_count=0,
        model_id="demo",
    )
    snapshot = Snapshot(
        epoch="epoch-demo",
        session_id=session.id,
        seq=0,
        session=session,
        messages=[],
        inputs=[],
        approvals=[],
        settings=SessionSettings(),
        config_revision=0,
        extensions=[],
        capabilities=["cancel", "continue"],
    )
    logical_input = InputView(
        id="input-demo",
        request_id="request-demo",
        run_id=None,
        logical_id="input-demo",
        status="queued",
        kind="input",
        created_at=1700000000001,
        requests_used=0,
        tokens_used=0,
        max_requests=8,
    )
    running = logical_input.model_copy(update={"run_id": "run-demo", "status": "running"})
    finished = running.model_copy(
        update={"status": "completed", "requests_used": 1, "tokens_used": 12}
    )
    user_message = MessageView(id="message-user", role="user", text="你好", timestamp=1700000000001)
    assistant = MessageView(
        id="message-assistant",
        role="assistant",
        text="",
        timestamp=1700000000002,
        status="streaming",
    )
    final_message = assistant.model_copy(update={"text": "你好，世界！", "status": "complete"})
    payloads: list[tuple[str, dict[str, Any]]] = [
        ("input_received", {"input": logical_input.model_dump(mode="json")}),
        ("message_end", {"message": user_message.model_dump(mode="json")}),
        ("run_started", {"input": running.model_dump(mode="json"), "status": "running"}),
        ("message_start", {"message": assistant.model_dump(mode="json")}),
        ("text_delta", {"message_id": assistant.id, "delta": "你好，"}),
        ("text_delta", {"message_id": assistant.id, "delta": "世界！"}),
        ("message_end", {"message": final_message.model_dump(mode="json")}),
        ("run_finished", {"input": finished.model_dump(mode="json"), "status": "completed"}),
    ]
    events = [
        WireEvent.model_validate(
            {
                "epoch": snapshot.epoch,
                "session_id": session.id,
                "seq": seq,
                "type": event_type,
                "payload": payload,
            }
        ).model_dump(mode="json")
        for seq, (event_type, payload) in enumerate(payloads, start=1)
    ]
    for event_type, payload in (
        ("heartbeat", {}),
        ("resync_required", {"reason": "snapshot_required"}),
    ):
        events.append(
            WireEvent.model_validate(
                {
                    "epoch": snapshot.epoch,
                    "session_id": session.id,
                    "seq": len(payloads),
                    "type": event_type,
                    "payload": payload,
                }
            ).model_dump(mode="json")
        )
    return {"snapshot": snapshot.model_dump(mode="json"), "events": events}
