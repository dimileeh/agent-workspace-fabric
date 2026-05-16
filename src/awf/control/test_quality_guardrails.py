"""Static guardrails against false-green Python tests."""

from __future__ import annotations

import ast
import fnmatch
import io
import os
import re
import tokenize
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

ViolationCode = Literal[
    "EMPTY_TEST",
    "FAKE_ASSERT",
    "SKIP_ONLY_TEST",
    "BROAD_MONKEYPATCH",
    "INVALID_ESCAPE_HATCH",
]

_SUPPRESSIBLE_CODES: Final[set[str]] = {
    "EMPTY_TEST",
    "FAKE_ASSERT",
    "SKIP_ONLY_TEST",
    "BROAD_MONKEYPATCH",
}
_DEFAULT_EXCLUDE_GLOBS: Final[tuple[str, ...]] = (
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.venv/**",
    "**/node_modules/**",
    "**/build/**",
    "**/dist/**",
)
_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"awf-test-quality:\s*ignore\[(?P<code>[A-Z_]+)\](?:\s+because\s+(?P<reason>.*))?$"
)
_VAGUE_RATIONALES: Final[tuple[str, ...]] = (
    "todo",
    "tbd",
    "wip",
    "temporary",
    "temp",
    "later",
    "fix later",
    "false positive",
    "known issue",
    "skip",
)
_MIN_ESCAPE_RATIONALE_WORDS: Final[int] = 3
_INFRA_CALL_ROOTS: Final[set[str]] = {
    "all",
    "any",
    "bool",
    "dict",
    "float",
    "int",
    "isinstance",
    "issubclass",
    "len",
    "list",
    "max",
    "min",
    "open",
    "print",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
}


@dataclass(frozen=True)
class TestQualityViolation:
    """A static quality violation found in a Python test file."""

    path: Path
    line: int
    code: ViolationCode
    message: str


@dataclass(frozen=True)
class _PendingViolation:
    path: Path
    line: int
    code: ViolationCode
    message: str
    test_start: int
    test_end: int

    def publish(self) -> TestQualityViolation:
        return TestQualityViolation(
            path=self.path,
            line=self.line,
            code=self.code,
            message=self.message,
        )


@dataclass(frozen=True)
class _EscapeHatch:
    line: int
    code: str
    reason: str
    valid: bool
    message: str


@dataclass(frozen=True)
class _MonkeypatchTarget:
    line: int
    dotted_name: str | None
    leaf: str


@dataclass(frozen=True)
class _CallRef:
    line: int
    dotted_name: str

    @property
    def leaf(self) -> str:
        return self.dotted_name.rsplit(".", maxsplit=1)[-1]

    @property
    def root(self) -> str:
        return self.dotted_name.split(".", maxsplit=1)[0]


def scan_test_quality(
    paths: Sequence[Path],
    *,
    exclude_globs: Sequence[str] = _DEFAULT_EXCLUDE_GLOBS,
) -> list[TestQualityViolation]:
    """Return focused false-green test quality violations for Python tests."""
    scan_root = _find_scan_root(paths)
    violations: list[TestQualityViolation] = []
    for path in _iter_python_files(paths, exclude_globs, scan_root):
        violations.extend(_scan_file(path))
    return sorted(
        violations, key=lambda violation: (str(violation.path), violation.line, violation.code)
    )


def _iter_python_files(
    paths: Sequence[Path],
    exclude_globs: Sequence[str],
    scan_root: Path | None,
) -> Iterator[Path]:
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            for candidate in sorted(path.rglob("*.py")):
                if not _is_excluded(candidate, exclude_globs, scan_root):
                    yield candidate
            continue
        if path.suffix == ".py" and not _is_excluded(path, exclude_globs, scan_root):
            yield path


def _find_scan_root(paths: Sequence[Path]) -> Path | None:
    anchors: list[Path] = []
    for raw_path in paths:
        resolved = Path(raw_path).resolve()
        anchor = resolved if resolved.is_dir() else resolved.parent
        anchors.append(anchor)
        if repo_root := _find_containing_repo_root(anchor):
            return repo_root
    if not anchors:
        return None
    return Path(os.path.commonpath([str(anchor) for anchor in anchors]))


def _find_containing_repo_root(anchor: Path) -> Path | None:
    for candidate in (anchor, *anchor.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _is_excluded(
    path: Path,
    exclude_globs: Sequence[str],
    scan_root: Path | None,
) -> bool:
    names = {path.as_posix()}
    if scan_root is not None:
        with suppress(ValueError):
            names.add(path.resolve().relative_to(scan_root).as_posix())
    return any(fnmatch.fnmatch(name, pattern) for name in names for pattern in exclude_globs)


def _scan_file(path: Path) -> list[TestQualityViolation]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    escape_hatches = _parse_escape_hatches(source)
    pending: list[_PendingViolation] = []
    for test in _iter_test_nodes(tree):
        pending.extend(_scan_test_node(path, test))
    return _apply_escape_hatches(path, pending, escape_hatches)


def _iter_test_nodes(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_test_name(node.name):
            yield node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and _is_test_name(
                    item.name
                ):
                    yield item


def _is_test_name(name: str) -> bool:
    return name.startswith("test_")


def _scan_test_node(
    path: Path,
    test: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[_PendingViolation]:
    violations: list[_PendingViolation] = []
    start_line = test.lineno
    end_line = test.end_lineno or test.lineno

    body = _body_without_docstring(test.body)
    effective_body = [statement for statement in body if not _is_noop_statement(statement)]
    if not effective_body:
        violations.append(
            _PendingViolation(
                path=path,
                line=start_line,
                code="EMPTY_TEST",
                message="test body contains no behavior or assertion",
                test_start=start_line,
                test_end=end_line,
            )
        )
    elif len(effective_body) == 1 and (skip_line := _skip_only_statement_line(effective_body[0])):
        violations.append(
            _PendingViolation(
                path=path,
                line=skip_line,
                code="SKIP_ONLY_TEST",
                message="test only calls pytest.skip without exercising behavior",
                test_start=start_line,
                test_end=end_line,
            )
        )

    for decorator in test.decorator_list:
        if _is_unconditional_skip_decorator(decorator):
            violations.append(
                _PendingViolation(
                    path=path,
                    line=decorator.lineno,
                    code="SKIP_ONLY_TEST",
                    message="test is unconditionally skipped by its pytest marker",
                    test_start=start_line,
                    test_end=end_line,
                )
            )

    for assert_node in _iter_asserts(test):
        if _is_fake_assert(assert_node):
            violations.append(
                _PendingViolation(
                    path=path,
                    line=assert_node.lineno,
                    code="FAKE_ASSERT",
                    message="test uses an exact assert True/False placeholder",
                    test_start=start_line,
                    test_end=end_line,
                )
            )

    violations.extend(_scan_broad_monkeypatches(path, test, start_line, end_line))
    return violations


def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if not body:
        return []
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _is_noop_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Expr):
        return isinstance(statement.value, ast.Constant) and statement.value.value is Ellipsis
    if isinstance(statement, ast.Return):
        return statement.value is None or (
            isinstance(statement.value, ast.Constant) and statement.value.value is None
        )
    return False


def _skip_only_statement_line(statement: ast.stmt) -> int | None:
    if _is_pytest_skip_expr(statement):
        return statement.lineno
    if not isinstance(statement, ast.If) or not _is_constant_true(statement.test):
        return None

    effective_body = [
        body_statement
        for body_statement in statement.body
        if not _is_noop_statement(body_statement)
    ]
    effective_else = [
        else_statement
        for else_statement in statement.orelse
        if not _is_noop_statement(else_statement)
    ]
    if len(effective_body) != 1 or effective_else:
        return None
    return _skip_only_statement_line(effective_body[0])


def _is_pytest_skip_expr(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _expr_to_dotted_name(statement.value.func) == "pytest.skip"
    )


def _is_unconditional_skip_decorator(decorator: ast.expr) -> bool:
    if _expr_to_dotted_name(decorator) == "pytest.mark.skip":
        return True
    if not isinstance(decorator, ast.Call):
        return False

    decorator_name = _expr_to_dotted_name(decorator.func)
    if decorator_name == "pytest.mark.skip":
        return True
    if decorator_name != "pytest.mark.skipif":
        return False

    if decorator.args:
        return _is_constant_true(decorator.args[0])
    condition = next(
        (keyword.value for keyword in decorator.keywords if keyword.arg == "condition"),
        None,
    )
    return condition is not None and _is_constant_true(condition)


def _is_constant_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _iter_asserts(test: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.Assert]:
    visitor = _AssertVisitor(test)
    visitor.visit(test)
    yield from visitor.asserts


class _AssertVisitor(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._root = root
        self.asserts: list[ast.Assert] = []

    def visit_Assert(self, node: ast.Assert) -> None:
        self.asserts.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self._root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self._root:
            self.generic_visit(node)

    def visit_ClassDef(self, _node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, _node: ast.Lambda) -> None:
        return


def _is_fake_assert(assert_node: ast.Assert) -> bool:
    return isinstance(assert_node.test, ast.Constant) and (
        assert_node.test.value is True or assert_node.test.value is False
    )


def _scan_broad_monkeypatches(
    path: Path,
    test: ast.FunctionDef | ast.AsyncFunctionDef,
    start_line: int,
    end_line: int,
) -> list[_PendingViolation]:
    monkeypatches = _collect_monkeypatch_targets(test)
    if not monkeypatches:
        return []

    calls = _collect_calls(test)
    violations: list[_PendingViolation] = []
    for target in monkeypatches:
        exercised_calls = [
            call for call in calls if call.line > target.line and not _is_infra_call(call)
        ]
        matching_calls = [
            call for call in exercised_calls if _call_matches_target(call, target, test.name)
        ]
        if not matching_calls:
            continue
        other_entrypoints = [
            call for call in exercised_calls if not _call_matches_target(call, target, test.name)
        ]
        if other_entrypoints:
            continue
        violations.append(
            _PendingViolation(
                path=path,
                line=target.line,
                code="BROAD_MONKEYPATCH",
                message=(
                    "test monkeypatches the same behavior it directly exercises; "
                    "patch a dependency or call the real production entrypoint"
                ),
                test_start=start_line,
                test_end=end_line,
            )
        )
    return violations


def _collect_monkeypatch_targets(
    test: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[_MonkeypatchTarget]:
    targets: list[_MonkeypatchTarget] = []
    for call in _collect_raw_calls(test):
        if _expr_to_dotted_name(call.func) != "monkeypatch.setattr":
            continue
        target = _extract_monkeypatch_target(call)
        if target is not None:
            targets.append(target)
    return targets


def _extract_monkeypatch_target(call: ast.Call) -> _MonkeypatchTarget | None:
    if not call.args:
        return None

    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        dotted_name = first.value
        leaf = dotted_name.rsplit(".", maxsplit=1)[-1]
        return _MonkeypatchTarget(line=call.lineno, dotted_name=dotted_name, leaf=leaf)

    if len(call.args) < 2:
        return None
    second = call.args[1]
    if not (isinstance(second, ast.Constant) and isinstance(second.value, str)):
        return None
    object_name = _expr_to_dotted_name(first)
    leaf = second.value
    target_name = f"{object_name}.{leaf}" if object_name is not None else None
    return _MonkeypatchTarget(line=call.lineno, dotted_name=target_name, leaf=leaf)


def _collect_calls(test: ast.FunctionDef | ast.AsyncFunctionDef) -> list[_CallRef]:
    calls: list[_CallRef] = []
    for call in _collect_raw_calls(test):
        dotted_name = _expr_to_dotted_name(call.func)
        if dotted_name is not None:
            calls.append(_CallRef(line=call.lineno, dotted_name=dotted_name))
    return calls


def _collect_raw_calls(test: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    visitor = _CallVisitor(test)
    visitor.visit(test)
    return visitor.calls


class _CallVisitor(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._root = root
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self._root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self._root:
            self.generic_visit(node)

    def visit_ClassDef(self, _node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, _node: ast.Lambda) -> None:
        return


def _is_infra_call(call: _CallRef) -> bool:
    if call.root in _INFRA_CALL_ROOTS:
        return True
    return call.dotted_name.startswith(("monkeypatch.", "pytest.", "mocker."))


def _call_matches_target(call: _CallRef, target: _MonkeypatchTarget, test_name: str) -> bool:
    if target.dotted_name is not None and call.dotted_name == target.dotted_name:
        return True
    if target.dotted_name is None:
        return call.leaf == target.leaf
    return call.leaf == target.leaf and _leaf_matches_test_subject(target.leaf, test_name)


def _leaf_matches_test_subject(leaf: str, test_name: str) -> bool:
    leaf_tokens = _identifier_tokens(leaf.removeprefix("_"))
    test_tokens = set(_identifier_tokens(test_name))
    return bool(leaf_tokens) and all(token in test_tokens for token in leaf_tokens)


def _identifier_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", value.lower()) if token]


def _expr_to_dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expr_to_dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _parse_escape_hatches(source: str) -> list[_EscapeHatch]:
    hatches: list[_EscapeHatch] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        comment = token.string.lstrip("#").strip()
        if "awf-test-quality:" not in comment:
            continue
        match = _ESCAPE_RE.search(comment)
        if match is None:
            hatches.append(
                _EscapeHatch(
                    line=token.start[0],
                    code="",
                    reason="",
                    valid=False,
                    message=(
                        "test-quality escape hatch must use "
                        "'awf-test-quality: ignore[CODE] because <specific rationale>'"
                    ),
                )
            )
            continue

        code = match.group("code")
        reason = (match.group("reason") or "").strip()
        invalid_reason = _invalid_escape_reason(code, reason)
        hatches.append(
            _EscapeHatch(
                line=token.start[0],
                code=code,
                reason=reason,
                valid=invalid_reason is None,
                message=invalid_reason or "valid escape hatch",
            )
        )
    return hatches


def _invalid_escape_reason(code: str, reason: str) -> str | None:
    if code not in _SUPPRESSIBLE_CODES:
        return f"test-quality escape hatch uses unknown rule code {code!r}"
    normalized = " ".join(reason.lower().split())
    if normalized in _VAGUE_RATIONALES:
        return "test-quality escape hatch rationale is too vague"
    if any(normalized.startswith(f"{vague} ") for vague in _VAGUE_RATIONALES):
        return "test-quality escape hatch rationale is too vague"
    if len(_identifier_tokens(reason)) < _MIN_ESCAPE_RATIONALE_WORDS:
        return "test-quality escape hatch needs a specific rationale after 'because'"
    return None


def _apply_escape_hatches(
    path: Path,
    pending: list[_PendingViolation],
    escape_hatches: list[_EscapeHatch],
) -> list[TestQualityViolation]:
    valid_hatches = [hatch for hatch in escape_hatches if hatch.valid]
    violations = [
        TestQualityViolation(
            path=path,
            line=hatch.line,
            code="INVALID_ESCAPE_HATCH",
            message=hatch.message,
        )
        for hatch in escape_hatches
        if not hatch.valid
    ]
    for violation in pending:
        if _is_suppressed(violation, valid_hatches):
            continue
        violations.append(violation.publish())
    return sorted(violations, key=lambda violation: (violation.line, violation.code))


def _is_suppressed(violation: _PendingViolation, hatches: list[_EscapeHatch]) -> bool:
    for hatch in hatches:
        if cast(str, violation.code) != hatch.code:
            continue
        if hatch.line in {violation.line, violation.line - 1}:
            return True
        if hatch.line == violation.test_start - 1:
            return True
    return False
