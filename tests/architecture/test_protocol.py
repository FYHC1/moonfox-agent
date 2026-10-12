"""One deterministic DTO/schema source, valid replay fixtures and optional transport imports."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from moonfox_app.server.export import protocol_document, protocol_fixtures
from moonfox_app.server.schema import (
    SCHEMAS,
    InputView,
    MessageView,
    Snapshot,
    Subscribe,
    UpdateConfig,
    WireEvent,
)

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "packages" / "web-ui" / "src" / "generated"
SERVER = ROOT / "packages" / "moonfox-app" / "src" / "moonfox_app" / "server"


@pytest.mark.parametrize(
    ("name", "factory"),
    [("protocol.json", protocol_document), ("fixtures.json", protocol_fixtures)],
)
def test_generated_protocol_is_fresh_and_deterministic(name, factory):
    first, second = factory(), factory()
    assert first == second
    expected = (json.dumps(first, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    assert (GENERATED / name).read_bytes() == expected


def test_protocol_document_contains_exact_standalone_pydantic_schemas():
    document = protocol_document()
    assert document["protocol_version"] == 1
    assert document["schemas"].keys() == SCHEMAS.keys()
    for name, model in SCHEMAS.items():
        schema = document["schemas"][name]
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert {key: value for key, value in schema.items() if key != "$schema"} == (
            model.model_json_schema()
        )


def test_fixtures_validate_and_reconstruct_a_stable_chinese_message():
    fixtures = protocol_fixtures()
    snapshot = Snapshot.model_validate_json(json.dumps(fixtures["snapshot"]))
    assert snapshot.seq == 0 and not snapshot.messages
    events = [WireEvent.model_validate_json(json.dumps(row)) for row in fixtures["events"]]
    kinds = {event.type for event in events}
    assert {
        "message_start",
        "text_delta",
        "message_end",
        "run_finished",
        "resync_required",
    } <= kinds
    messages, text, seq = {}, {}, snapshot.seq
    for event in events:
        assert event.epoch == snapshot.epoch and event.session_id == snapshot.session_id
        if event.type not in {"heartbeat", "resync_required"}:
            assert event.seq == seq + 1
            seq = event.seq
        else:
            assert event.seq == seq
        if event.type in {"message_start", "message_end"}:
            message = MessageView.model_validate(event.payload["message"])
            messages[message.id] = message
            if event.type == "message_start":
                text[message.id] = message.text
            elif message.role == "assistant":
                assert text[message.id] == message.text
        elif event.type == "text_delta":
            text[event.payload["message_id"]] += event.payload["delta"]
        elif event.type in {"input_received", "run_started", "run_finished"}:
            InputView.model_validate(event.payload["input"])
    assert len(messages) == 2
    assert messages["message-assistant"].text == "你好，世界！"
    assert messages["message-assistant"].status == "complete"


def test_dto_strict_types_with_json_native_settings_arrays():
    body = {
        "request_id": "r",
        "expected_revision": 0,
        "settings": {"allowed_tools": ["echo"], "approval_tools": ["echo"]},
    }
    config = UpdateConfig.model_validate_json(json.dumps(body))
    assert config.settings.allowed_tools == ("echo",)
    for revision in (True, "0", 0.0, -1):
        with pytest.raises(ValidationError):
            UpdateConfig.model_validate_json(json.dumps({**body, "expected_revision": revision}))
    for after_seq in (False, "0", 0.0, -1):
        with pytest.raises(ValidationError):
            Subscribe.model_validate_json(
                json.dumps({"token": "token", "epoch": "epoch", "after_seq": after_seq})
            )


def test_export_check_is_cwd_independent_and_has_no_sys_path_injection(tmp_path):
    script = ROOT / "scripts" / "export_protocol.py"
    tree = ast.parse(script.read_text("utf-8"))
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "path"
        for node in ast.walk(tree)
    )
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_headless_import_and_protocol_export_do_not_require_web_dependencies(tmp_path):
    code = """
import importlib.abc
import sys
class NoWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'starlette', 'uvicorn', 'httpx', 'websockets'}:
            raise AssertionError('Unexpected web dependency: ' + fullname)
sys.meta_path.insert(0, NoWeb())
import moonfox_app
import moonfox_app.api
import moonfox_app.server
from moonfox_app.server.export import protocol_document, protocol_fixtures
assert protocol_document()['protocol_version'] == 1
assert protocol_fixtures()['snapshot']['messages'] == []
assert 'moonfox_app.server.app' not in sys.modules
"""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_server_does_not_import_runtime_bootstrap_or_agent_state():
    for path in SERVER.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                targets = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [node.module or "", *(name.name for name in node.names)]
            else:
                continue
            assert not any(
                "runtime" in target.split(".")
                or "bootstrap" in target.split(".")
                or target == "AgentState"
                for target in targets
            ), path
