"""Trusted diagnostic challenge/verifier; installed outside agent-writable files.

The controller runs verification only after the CLI exits, compares the result
outside the candidate process, and never executes workspace-authored tests.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

FIXTURE_VERSION = "coding-cycle-v1"
RUNTIME_FIXTURE_PATH = "/opt/awf/provider_diagnostic_fixture.py"
PROMPT = """AWF provider connection diagnostic. Work only in this fixed fixture.
First use your shell tool to execute exactly: awf-provider-challenge
It returns a fresh AWF_NONCE. Wait for that tool result. In a later, distinct
tool call whose input includes the returned nonce literally, edit solution.py
so answer(value) returns that nonce + ":" + value reversed. Use one pure function,
optional literal string constants, concatenation or an f-string and a reverse
slice. No imports, function calls, decorators or executable module statements.
Exercise the code in your shell.
Do not combine the challenge and edit in one tool call. Do not change other files.
Do not commit, push, invoke another agent, or contact a forge. Finish after testing.
"""
_VERIFIER = """import ast, json, sys
from pathlib import Path
source = Path(sys.argv[1]).read_text()
tree = ast.parse(source)
allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
    ast.BinOp, ast.Add, ast.Subscript, ast.Slice, ast.UnaryOp, ast.USub,
    ast.Constant, ast.Name, ast.Load, ast.Store, ast.Assign, ast.Expr,
    ast.JoinedStr, ast.FormattedValue)
nodes = list(ast.walk(tree))
assert len(nodes) <= 200 and all(isinstance(n, allowed) for n in nodes)
assert all(not isinstance(n, ast.Name) or not n.id.startswith("__") for n in nodes)
assert all(not isinstance(n, ast.Constant) or not isinstance(n.value, str) or len(n.value) <= 512 for n in nodes)
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
assert len(functions) == 1 and functions[0].name == "answer"
assert all(isinstance(n, (ast.FunctionDef, ast.Assign, ast.Expr)) for n in tree.body)
assert all(not isinstance(n, ast.Assign) or isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) for n in nodes)
assert all(not isinstance(n, ast.Expr) or isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) for n in nodes)
assert all(not isinstance(n, ast.FormattedValue) or n.format_spec is None for n in nodes)
namespace = {"__builtins__": {}, "str": str}
exec(compile(tree, sys.argv[1], "exec"), namespace)
print(json.dumps([namespace["answer"]("awf"), namespace["answer"]("second")]))
"""


def challenge() -> None:
    """Generate a nonce during the tool execution, never in its initial prompt."""
    print("AWF_NONCE=" + secrets.token_hex(16), flush=True)


def seed_fixture(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "solution.py").write_text(
        'def answer(value):\n    raise NotImplementedError("implement the challenge")\n'
    )


def verify_fixture(root: Path, nonce: str, *, timeout: float = 5) -> bool:
    candidate = root / "solution.py"
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 65_536:
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _VERIFIER, str(candidate.resolve())],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/",
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            check=False,
        )
        return result.returncode == 0 and json.loads(result.stdout) == [
            nonce + ":fwa",
            nonce + ":dnoces",
        ]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def runtime_is_immutable(*, path: Path | None = None) -> bool:
    path = path or Path(__file__).resolve()
    interpreter = Path(sys.executable).resolve()
    try:
        return os.geteuid() != 0 and all(
            item.stat().st_uid == 0 and not os.access(item, os.W_OK)
            for item in (path, path.parent, interpreter, interpreter.parent)
        )
    except OSError:
        return False


def main(argv: list[str]) -> int:
    if argv == ["--contract-version"]:
        print(FIXTURE_VERSION)
        return 0
    if argv == ["--capability"]:
        if not runtime_is_immutable():
            return 78
        print(FIXTURE_VERSION)
        return 0
    if argv == ["challenge"]:
        challenge()
        return 0
    if len(argv) == 3 and argv[0] == "verify":
        return 0 if verify_fixture(Path(argv[1]), argv[2]) else 1
    return 64


if __name__ == "__main__":  # pragma: no cover - process wiring; main is tested directly
    raise SystemExit(main(sys.argv[1:]))
