"""Executable package boundaries; scan imports, metadata, and domain vocabulary."""

import ast
import re
import sys
import tomllib
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    "moonfox_ai": ROOT / "packages" / "ai",
    "moonfox_agent": ROOT / "packages" / "agent",
}
EXTERNAL_IMPORTS = {"moonfox_ai": set(), "moonfox_agent": {"pydantic"}}
DISTRIBUTIONS = {"moonfox_ai": set(), "moonfox_agent": {"moonfox-ai", "pydantic"}}
DOMAIN_WORDS = re.compile(r"persona|memory|qq|moonfox_app", re.IGNORECASE)


def import_violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    namespace = module.split(".")[0]
    package = module if is_package else module.rpartition(".")[0]
    violations = []
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
                    violations.append(f"{node.lineno}: relative import escapes package: {target}")
                    continue
            targets = [target]
            # Include submodule imports (not just the containing package).
            if target in PACKAGES:
                targets.extend(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "__import__":
                violations.append(f"{node.lineno}: dynamic imports are forbidden in core")

        for target in targets:
            root = target.split(".")[0]
            if root == "importlib":
                violations.append(f"{node.lineno}: dynamic loaders are forbidden in core")
            elif root == namespace:
                continue
            elif namespace == "moonfox_agent" and target == "moonfox_ai.api":
                continue
            elif namespace == "moonfox_agent" and target == "moonfox_ai":
                # The following imported names are checked separately above.
                continue
            elif root in sys.stdlib_module_names or root in EXTERNAL_IMPORTS[namespace]:
                continue
            else:
                violations.append(f"{node.lineno}: {namespace} must not import {target}")
    return violations


@pytest.mark.parametrize("namespace", PACKAGES)
def test_source_imports_follow_layering(namespace):
    source_root = PACKAGES[namespace] / "src"
    paths = sorted((source_root / namespace).rglob("*.py"))
    assert paths, f"Missing source package: {namespace}"
    failures = []
    for path in paths:
        parts = path.relative_to(source_root).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if is_package else parts)
        failures.extend(
            f"{path.relative_to(ROOT)}:{violation}"
            for violation in import_violations(
                path.read_text(encoding="utf-8"), module, is_package=is_package
            )
        )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("namespace", PACKAGES)
def test_declared_dependencies_follow_layering(namespace):
    project = tomllib.loads((PACKAGES[namespace] / "pyproject.toml").read_text("utf-8"))["project"]
    dependencies = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    requirements = [*dependencies, *(req for group in optional.values() for req in group)]
    for requirement in requirements:
        name = re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0]
        name = re.sub(r"[-_.]+", "-", name).lower()
        assert "@" not in requirement, f"Direct dependency URL is not allowed: {requirement}"
        assert name in DISTRIBUTIONS[namespace], f"Forbidden dependency: {requirement}"
    if namespace == "moonfox_ai":
        assert dependencies == []
    else:
        ai_project = tomllib.loads((PACKAGES["moonfox_ai"] / "pyproject.toml").read_text("utf-8"))
        assert f"moonfox-ai=={ai_project['project']['version']}" in dependencies


@pytest.mark.parametrize("namespace", PACKAGES)
def test_core_source_has_no_domain_vocabulary(namespace):
    for path in (PACKAGES[namespace] / "src").rglob("*.py"):
        assert not DOMAIN_WORDS.search(path.read_text("utf-8")), str(path.relative_to(ROOT))


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import moonfox_agent", "moonfox_ai.types"),
        ("from moonfox_app import api", "moonfox_agent.agent"),
        ("from moonfox_ai.providers.faux import FauxProvider", "moonfox_agent.agent"),
        ("from moonfox_ai import types", "moonfox_agent.agent"),
        ("from moonfox_ext_qq import plugin", "moonfox_agent.agent"),
        ("import fastapi", "moonfox_ai.types"),
        ("from ...moonfox_app import api", "moonfox_ai.providers.base"),
        ("__import__('moonfox_app')", "moonfox_agent.agent"),
        ("__import__(name)", "moonfox_agent.agent"),
        ("import importlib as loader\nloader.import_module(name)", "moonfox_agent.agent"),
        ("from importlib import import_module as load\nload(name)", "moonfox_agent.agent"),
        ("if TYPE_CHECKING:\n    import moonfox_app", "moonfox_agent.agent"),
    ],
)
def test_forbidden_imports_are_detected(source, module):
    assert import_violations(source, module)


@pytest.mark.parametrize(
    ("source", "module", "is_package"),
    [
        ("from .types import Message", "moonfox_ai.transcript", False),
        ("from ..types import Model", "moonfox_ai.providers.faux", False),
        ("from . import types", "moonfox_ai", True),
        ("from moonfox_ai.api import Model", "moonfox_agent.types", False),
        ("from pydantic import BaseModel", "moonfox_agent.types", False),
        ("import asyncio\nfrom collections.abc import Callable", "moonfox_ai.stream", False),
    ],
)
def test_allowed_imports_are_accepted(source, module, is_package):
    assert import_violations(source, module, is_package=is_package) == []
