"""The runtime must not be able to run commands or code, whatever a model asks for."""

import ast
from pathlib import Path

import agent_runtime

PACKAGE = Path(agent_runtime.__file__).parent
FORBIDDEN_MODULES = {"subprocess", "pty", "shlex", "multiprocessing", "ctypes", "pickle", "marshal"}
# Builtins, matched as bare names.
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__", "breakpoint"}
# Functions of os, asyncio, and importlib, matched as attributes or imported names.
FORBIDDEN_FUNCTIONS = {
    "system",
    "popen",
    "spawnl",
    "spawnv",
    "spawnve",
    "execv",
    "execve",
    "execvp",
    "fork",
    "forkpty",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "import_module",
}


def _violations(source: Path) -> list[str]:
    tree = ast.parse(source.read_text(), filename=str(source))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.partition(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").partition(".")[0]]
            names += [alias.name for alias in node.names if alias.name in FORBIDDEN_FUNCTIONS]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names = [node.func.id] if node.func.id in FORBIDDEN_BUILTINS else []
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names = [node.func.attr] if node.func.attr in FORBIDDEN_FUNCTIONS else []
        else:
            continue
        found += [
            f"{source.relative_to(PACKAGE)}:{node.lineno}: {name}"
            for name in names
            if name in FORBIDDEN_MODULES | FORBIDDEN_BUILTINS | FORBIDDEN_FUNCTIONS
        ]
    return found


def test_the_package_cannot_start_processes_or_evaluate_code() -> None:
    sources = sorted(PACKAGE.rglob("*.py"))

    assert sources
    assert [v for source in sources for v in _violations(source)] == []
