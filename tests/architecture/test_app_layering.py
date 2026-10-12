"""Enforce the application's internal dependency direction, including relative imports."""

import ast
import re
import sys
import tomllib
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "packages" / "moonfox-app"
ALLOWED_EXTERNAL = {"pydantic", "packaging"}


def violations(source, module, is_package=False):
    package = module if is_package else module.rpartition(".")[0]
    layer = module.split(".")[1] if "." in module else ""
    forbidden = {
        "extension_api": {
            "bootstrap",
            "runtime",
            "extensions",
            "services",
            "config",
            "security",
            "jobs",
            "server",
        },
        "runtime": {"bootstrap", "extensions", "services", "config", "security", "jobs", "server"},
        "extensions": {"bootstrap", "runtime", "services", "config", "security", "jobs", "server"},
        "services": {"bootstrap", "server"},
        "config": {"bootstrap", "runtime", "extensions", "services", "security", "jobs", "server"},
        "security": {"bootstrap", "runtime", "extensions", "services", "config", "jobs", "server"},
        "server": {"bootstrap", "runtime", "extensions"},
    }.get(layer, set())
    errors = []
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            target = "." * node.level + (node.module or "")
            if node.level:
                try:
                    target = resolve_name(target, package)
                except (ImportError, ValueError):
                    errors.append("relative escape")
                    continue
            targets = [target, *(f"{target}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "__import__":
                errors.append("dynamic import")
        for target in targets:
            root = target.split(".")[0]
            if root == "moonfox_app":
                part = target.split(".")[1] if "." in target else ""
                if part in forbidden or (layer in forbidden_by_api and part == "api"):
                    errors.append(target)
            elif root in {"moonfox_ai", "moonfox_agent"}:
                if target != root and not target.startswith(root + ".api"):
                    errors.append(target)
            elif root == "importlib":
                errors.append("dynamic loader")
            elif root in {"fastapi", "starlette", "uvicorn", "websockets"} and layer in {
                "server",
                "cli",
            }:
                continue
            elif root == "httpx" and module == "moonfox_app.services.http_transport":
                continue
            elif root not in sys.stdlib_module_names and root not in ALLOWED_EXTERNAL:
                errors.append(target)
    return errors


forbidden_by_api = {
    "extension_api",
    "runtime",
    "extensions",
    "services",
    "security",
    "config",
    "server",
}


def test_app_imports_follow_internal_boundaries():
    failures = []
    for path in (APP / "src" / "moonfox_app").rglob("*.py"):
        parts = path.relative_to(APP / "src").with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if is_package else parts)
        failures.extend(
            f"{path}: {error}" for error in violations(path.read_text("utf-8"), module, is_package)
        )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("from ...services import hub", "moonfox_app.runtime.session.manager"),
        ("from moonfox_app import bootstrap", "moonfox_app.extension_api.types"),
        ("from moonfox_app.runtime import session", "moonfox_app.extensions.manager"),
        ("from moonfox_app import api", "moonfox_app.runtime.session.manager"),
        ("import moonfox_ext_memory", "moonfox_app.runtime.prompt.pipeline"),
        ("from moonfox_agent.agent import Agent", "moonfox_app.runtime.session.manager"),
        ("import importlib", "moonfox_app.extension_api.types"),
        ("__import__(name)", "moonfox_app.runtime.session.manager"),
        ("import fastapi", "moonfox_app.runtime.session.manager"),
        ("from moonfox_app.bootstrap import create_headless_app", "moonfox_app.services.hub"),
        ("from moonfox_app.server import schema", "moonfox_app.services.hub"),
        ("from moonfox_app.runtime.session import manager", "moonfox_app.server.app"),
        ("import fastapi", "moonfox_app.security.identity"),
        ("import httpx", "moonfox_app.runtime.session.manager"),
        ("import httpx", "moonfox_app.services.hub"),
    ],
)
def test_app_forbidden_import_negative_cases(source, module):
    assert violations(source, module)


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("from ...extension_api import SessionEvent", "moonfox_app.runtime.session.events"),
        ("from moonfox_agent.api import Agent", "moonfox_app.runtime.session.manager"),
        ("from .runtime.session import manager", "moonfox_app.bootstrap"),
        ("from .capabilities import TaskService", "moonfox_app.extension_api"),
        ("import httpx", "moonfox_app.services.http_transport"),
    ],
)
def test_app_allowed_import_cases(source, module):
    assert not violations(source, module, module.endswith("extension_api"))


def test_headless_distribution_has_no_web_or_business_dependencies():
    project = tomllib.loads((APP / "pyproject.toml").read_text("utf-8"))["project"]
    dependencies = project["dependencies"]
    names = {re.split(r"[\[<>=!~;@]", req, maxsplit=1)[0] for req in dependencies}
    assert names == {"moonfox-ai", "moonfox-agent-core", "pydantic", "packaging"}
    assert "moonfox-ai==0.1.0" in dependencies
    assert "moonfox-agent-core==0.1.0" in dependencies
    assert set(project["optional-dependencies"]) == {"server", "providers"}
    assert project["optional-dependencies"]["providers"] == ["httpx>=0.28,<1"]
    assert {re.split(r"[<>=]", req)[0] for req in project["optional-dependencies"]["server"]} == {
        "fastapi",
        "uvicorn",
        "websockets",
    }


def test_example_plugin_uses_only_sdk():
    path = ROOT / "examples" / "minimal_extension" / "plugin.py"
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("moonfox_"):
            assert node.module == "moonfox_app.extension_api"
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("moonfox_app.") for alias in node.names)
