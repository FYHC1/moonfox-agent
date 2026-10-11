"""Opt-in real distributions check; package downloads need an index or a populated cache."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.installation,
    pytest.mark.skipif(
        os.environ.get("MOONFOX_RUN_INSTALLATION") != "1",
        reason="opt-in: python scripts/check.py --installation",
    ),
]


def run(command, *, cwd, env):
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    assert completed.returncode == 0, (
        f"Command failed: {command}\n{completed.stdout}\n{completed.stderr}"
    )
    return completed.stdout


def make_environment(path):
    venv.EnvBuilder(with_pip=True).create(path)
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


AI_SMOKE = """
import asyncio
import importlib.util
import json
import sys
from importlib.metadata import distributions
from pathlib import Path
from moonfox_ai.api import Context, FauxProvider, FauxResponse, Model, normalize_context
import moonfox_ai.api
assert Path(moonfox_ai.api.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
for module in ('moonfox_agent', 'moonfox_app', 'pydantic', 'fastapi', 'uvicorn'):
    assert importlib.util.find_spec(module) is None, module
async def main():
    faux = FauxProvider([FauxResponse.text('standalone')])
    stream = faux.stream(Model('test', 'faux', 'faux'), normalize_context(Context([])))
    result = await stream.result()
    assert result.content[0].text == 'standalone'
    await stream.aclose()
asyncio.run(main())
print(json.dumps(sorted(d.metadata['Name'].lower().replace('_', '-') for d in distributions())))
"""

CORE_SMOKE = """
import importlib.util
import json
import sys
from importlib.metadata import distributions
from pathlib import Path
import moonfox_ai.api
import moonfox_agent.api
for module in (moonfox_ai.api, moonfox_agent.api):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
for name in ('moonfox_app', 'moonfox_ext_persona', 'moonfox_ext_memory', 'moonfox_ext_qq',
             'fastapi', 'uvicorn', 'websockets', 'httpx', 'aiosqlite'):
    assert importlib.util.find_spec(name) is None, name
print(json.dumps(sorted(d.metadata['Name'].lower().replace('_', '-') for d in distributions())))
"""


def test_core_wheels_and_source_installs_work_outside_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    # Keep inherited TEMP/TMP shallow: pip's hashed wheel cache can exceed MAX_PATH
    # when placed below pytest's long per-test directory on Windows.

    wheels = {}
    for package, namespace in (("ai", "moonfox_ai"), ("agent", "moonfox_agent")):
        # Separate sdist builds prove each package owns all its build inputs.
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--sdist",
                "--no-isolation",
                "--outdir",
                artifacts,
                ROOT / "packages" / package,
            ],
            cwd=outside,
            env=env,
        )
        distribution = "moonfox_ai" if package == "ai" else "moonfox_agent_core"
        archive = next(artifacts.glob(f"{distribution}-*.tar.gz"))
        extracted = tmp_path / f"source-{package}"
        extracted.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter="data")
        source_root = next(extracted.iterdir())
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                artifacts,
                source_root,
            ],
            cwd=outside,
            env=env,
        )
        wheels[package] = next(artifacts.glob(f"{distribution}-*.whl"))
        with zipfile.ZipFile(wheels[package]) as wheel:
            paths = wheel.namelist()
            assert f"{namespace}/py.typed" in paths
            assert f"{namespace}/api.py" in paths
            assert all(path.startswith((f"{namespace}/", f"{distribution}-")) for path in paths)
            metadata_path = next(path for path in paths if path.endswith(".dist-info/METADATA"))
            metadata = Parser().parsestr(wheel.read(metadata_path).decode("utf-8"))
            requirements = metadata.get_all("Requires-Dist", [])
            if package == "ai":
                assert requirements == []
            else:
                assert sorted(req.split("<")[0].split("=")[0] for req in requirements) == [
                    "moonfox-ai",
                    "pydantic",
                ]

    # Materialize the runtime and isolated-build dependencies once; installs below are offline.
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            artifacts,
            "--find-links",
            artifacts,
            wheels["agent"],
            "hatchling>=1.26,<2",
        ],
        cwd=outside,
        env=env,
    )
    env["PIP_NO_INDEX"] = "1"
    env["PIP_FIND_LINKS"] = str(artifacts)

    ai_python = make_environment(tmp_path / "ai-only")
    run([ai_python, "-I", "-m", "pip", "install", wheels["ai"]], cwd=outside, env=env)
    installed = json.loads(run([ai_python, "-I", "-c", AI_SMOKE], cwd=outside, env=env))
    assert set(installed) == {"pip", "moonfox-ai"}
    run([ai_python, "-I", "-m", "pip", "check"], cwd=outside, env=env)

    core_python = make_environment(tmp_path / "core-only")
    # Agent's metadata must resolve the AI wheel without an explicit AI install command.
    run([core_python, "-I", "-m", "pip", "install", wheels["agent"]], cwd=outside, env=env)
    installed = json.loads(run([core_python, "-I", "-c", CORE_SMOKE], cwd=outside, env=env))
    expected_runtime = {
        "pip",
        "moonfox-ai",
        "moonfox-agent-core",
        "pydantic",
        "pydantic-core",
        "annotated-types",
        "typing-extensions",
        "typing-inspection",
    }
    assert set(installed) == expected_runtime
    example = outside / "core_only.py"
    shutil.copy2(ROOT / "examples" / "core_only.py", example)
    output = run([core_python, "-I", example], cwd=outside, env=env)
    assert "2 + 3 = 5" in output
    assert "reason=completed; turns=2; requests=2" in output
    run([core_python, "-I", "-m", "pip", "check"], cwd=outside, env=env)

    # Also exercise the handoff's exact local-directory install workflow, not editable installs.
    source_python = make_environment(tmp_path / "local-installs")
    for package in ("ai", "agent"):
        run(
            [source_python, "-I", "-m", "pip", "install", ROOT / "packages" / package],
            cwd=outside,
            env=env,
        )
    installed = json.loads(run([source_python, "-I", "-c", CORE_SMOKE], cwd=outside, env=env))
    assert set(installed) == expected_runtime
    assert "requests=2" in run([source_python, "-I", example], cwd=outside, env=env)
    run([source_python, "-I", "-m", "pip", "check"], cwd=outside, env=env)
