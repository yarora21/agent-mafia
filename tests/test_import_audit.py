"""PR2 gate: the engine imports no network or LLM libraries.

This runs from the first engine PR onward. It parses every module under
``engine/`` and asserts none of them import a forbidden top-level package. It is
the automated guard behind the spec's "no LLM calls in engine/" rule — it fails
in CI the moment someone reaches for an API client inside the pure core.
"""

import ast
from pathlib import Path

import engine

FORBIDDEN_ROOTS = {
    # LLM / agent frameworks
    "anthropic",
    "openai",
    "langgraph",
    "langchain",
    "langchain_anthropic",
    # network stacks
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "urllib3",
    "socket",
    "http",
    "websockets",
    # agent-layer packages (engine must not depend upward)
    "agents",
}

ENGINE_DIR = Path(engine.__file__).parent


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Ignore relative imports (node.level > 0); they stay inside engine.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_engine_modules_are_pure() -> None:
    modules = sorted(ENGINE_DIR.glob("*.py"))
    assert modules, "expected engine/ to contain modules"
    for path in modules:
        roots = _imported_roots(path.read_text())
        offenders = roots & FORBIDDEN_ROOTS
        assert not offenders, f"{path.name} imports forbidden package(s): {offenders}"
