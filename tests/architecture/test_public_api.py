import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "moonfox_ai.api",
        "moonfox_agent.api",
        "moonfox_app.api",
        "moonfox_app.extension_api",
    ],
)
def test_public_import_has_no_startup_side_effects(module, tmp_path):
    code = f"""
import asyncio
import importlib
import threading
import sys
from unittest.mock import patch
async def check():
    before_tasks = asyncio.all_tasks()
    before_threads = threading.enumerate()
    with (
        patch('socket.socket', side_effect=AssertionError('network on import')),
        patch('subprocess.Popen', side_effect=AssertionError('process on import')),
        patch('threading.Thread.start', side_effect=AssertionError('thread on import')),
        patch('asyncio.create_task', side_effect=AssertionError('task on import')),
    ):
        api = importlib.import_module('{module}')
    assert len(api.__all__) == len(set(api.__all__))
    assert all(hasattr(api, name) for name in api.__all__)
    assert asyncio.all_tasks() == before_tasks
    assert threading.enumerate() == before_threads
    assert not any(name.startswith(('fastapi', 'uvicorn', 'httpx', 'moonfox_ext_'))
                   for name in sys.modules)
    if '{module}' == 'moonfox_app.extension_api':
        assert not any(name.startswith(('moonfox_app.bootstrap', 'moonfox_app.runtime',
                                       'moonfox_app.extensions', 'moonfox_app.server'))
                       for name in sys.modules)
asyncio.run(check())
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
