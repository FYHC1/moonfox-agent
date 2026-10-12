"""Business implementations consume only SDK contracts, not host internals."""

import ast
import os
import re
import subprocess
import sys
import tomllib
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["persona", "memory"])
def test_business_extension_source_and_distribution_boundary(name):
    root = ROOT / "extensions" / name
    namespace = f"moonfox_ext_{name}"
    project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["name"] == f"moonfox-ext-{name}"
    assert {re.split(r"[<>=!~;\[]", dep)[0] for dep in project["dependencies"]} == {
        "moonfox-app",
        "pydantic",
    }
    assert (root / "src" / namespace / "py.typed").exists()
    for path in (root / "src").rglob("*.py"):
        module = ".".join(path.relative_to(root / "src").with_suffix("").parts)
        package = module.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                target = "." * node.level + (node.module or "")
                targets = [resolve_name(target, package) if node.level else target]
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "__import__", path
            for target in targets:
                assert target == "moonfox_app.extension_api" or target.split(".")[0] in (
                    sys.stdlib_module_names | {"pydantic", namespace}
                ), (path, target)
                assert not target.startswith("importlib"), path


@pytest.mark.parametrize("name", ["persona", "memory"])
def test_importing_business_extension_does_not_start_host_or_create_storage(name, tmp_path):
    code = f"""
import asyncio, importlib, sys, threading
from unittest.mock import patch
async def check():
    before = asyncio.all_tasks()
    threads = threading.enumerate()
    with (
        patch('socket.socket', side_effect=AssertionError('network on import')),
        patch('subprocess.Popen', side_effect=AssertionError('process on import')),
        patch('threading.Thread.start', side_effect=AssertionError('thread on import')),
        patch('asyncio.create_task', side_effect=AssertionError('task on import')),
        patch('sqlite3.connect', side_effect=AssertionError('database on import')),
    ):
        api = importlib.import_module('moonfox_ext_{name}.api')
    assert all(hasattr(api, item) for item in api.__all__)
    assert len(set(api.__all__)) == len(api.__all__)
    assert asyncio.all_tasks() == before and threading.enumerate() == threads
    assert not any(key.startswith(('moonfox_app.bootstrap', 'moonfox_app.runtime',
        'moonfox_app.services', 'moonfox_app.server', 'fastapi', 'httpx')) for key in sys.modules)
asyncio.run(check())
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.iterdir())
