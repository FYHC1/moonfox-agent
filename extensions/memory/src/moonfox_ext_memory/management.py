"""JSON-only management commands; every invocation opens exactly one bound store."""

from __future__ import annotations

import json
from typing import Any

from moonfox_app.extension_api import ExtensionResources

from .settings import (
    ClearArguments,
    GetArguments,
    JobsArguments,
    ListArguments,
    RecordRevisionArguments,
    RememberArguments,
    StrictArguments,
    UpdateArguments,
)
from .storage import open_scoped_store

COMMANDS = ("list", "get", "remember", "update", "forget", "delete", "clear", "jobs")
_REMOVAL_NOTICE = (
    "Memory extension data and recall snapshots were removed from this scope. "
    "Original chat history, previously written tool results, provider-side copies, "
    "and external backups are not removed."
)
_ARGUMENT_MODELS: dict[str, type[StrictArguments]] = {
    "list": ListArguments,
    "get": GetArguments,
    "remember": RememberArguments,
    "update": UpdateArguments,
    "forget": RecordRevisionArguments,
    "delete": RecordRevisionArguments,
    "clear": ClearArguments,
    "jobs": JobsArguments,
}


def _json_object(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


async def manage(resources: ExtensionResources, command: str, arguments_json: str) -> str:
    """Run one strict command without attaching a session or invoking a model/task."""
    if command not in COMMANDS:
        raise ValueError("Unknown memory management command")
    if not isinstance(arguments_json, str) or len(arguments_json) > 32_000:
        raise ValueError("Management arguments must be bounded JSON")
    argument_model = _ARGUMENT_MODELS[command]
    arguments = argument_model.model_validate_json(arguments_json)

    with open_scoped_store(resources) as store:
        if command == "list":
            assert isinstance(arguments, ListArguments)
            revision, records = store.list_with_revision(
                arguments.query,
                arguments.limit,
                include_forgotten=arguments.include_forgotten,
            )
            return _json_object(
                {"records": [record.to_dict() for record in records], "revision": revision}
            )
        if command == "get":
            assert isinstance(arguments, GetArguments)
            return _json_object({"record": store.get(arguments.id).to_dict()})
        if command == "remember":
            assert isinstance(arguments, RememberArguments)
            record = store.remember(
                arguments.text,
                arguments.kind,
                arguments.subject,
                origin="manual",
            )
            return _json_object({"record": record.to_dict()})
        if command == "update":
            assert isinstance(arguments, UpdateArguments)
            record = store.update(
                arguments.id,
                arguments.expected_revision,
                arguments.text,
                arguments.kind,
                arguments.subject,
            )
            return _json_object({"record": record.to_dict()})
        if command == "forget":
            assert isinstance(arguments, RecordRevisionArguments)
            return _json_object(
                {"record": store.forget(arguments.id, arguments.expected_revision).to_dict()}
            )
        if command == "delete":
            assert isinstance(arguments, RecordRevisionArguments)
            store.delete(arguments.id, arguments.expected_revision)
            return _json_object({"deleted": True, "notice": _REMOVAL_NOTICE})
        if command == "clear":
            assert isinstance(arguments, ClearArguments)
            revision = store.clear(arguments.expected_revision)
            return _json_object({"deleted": True, "revision": revision, "notice": _REMOVAL_NOTICE})
        assert command == "jobs" and isinstance(arguments, JobsArguments)
        jobs = store.list_jobs()
        return _json_object(
            {
                "jobs": [
                    {
                        "id": job.id,
                        "status": job.status,
                        "created_at": job.created_at,
                        "updated_at": job.updated_at,
                    }
                    for job in jobs
                ]
            }
        )
