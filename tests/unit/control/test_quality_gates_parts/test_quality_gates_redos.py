"""ReDoS hardening regressions for the ``quality_gates*`` validation regexes.

CodeQL flagged ``_VALIDATION_TEST_COMMAND_RE`` and ``_VALIDATION_TEST_PATH_RE``
(``py/redos`` / ``py/polynomial-redos``) in all five ``quality_gates*`` modules.
Both carry a repeated separator group whose greedy inner token class overlaps an
adjacent ``\\s+`` boundary, so a long run of flag-like tokens has exponentially
many ways to split → catastrophic backtracking.

The five modules carry byte-identical copies of these constants, so the same
accept-set + timing assertions run against every module's compiled pattern.
"""

from __future__ import annotations

import re
import time
from types import ModuleType

import pytest

from awf.control import quality_gates as _qg
from awf.control import quality_gates_pyproject as _qg_pyproject
from awf.control import quality_gates_workflow as _qg_workflow
from awf.control import quality_gates_workflow_actions as _qg_actions
from awf.control import quality_gates_workflow_commands as _qg_commands

_MODULES: tuple[ModuleType, ...] = (
    _qg,
    _qg_pyproject,
    _qg_workflow,
    _qg_actions,
    _qg_commands,
)

# Embedded copies of the ORIGINAL (pre-hardening) patterns. Kept local to the
# test so the differential guard proves equivalence without re-shipping the
# vulnerable regexes from production.
_ORIGINAL_TEST_COMMAND_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:npm|pnpm|yarn|bun|go|cargo|make|mvn|gradle|gradlew|tox|nox|uv|poetry|pipenv)"
    r"(?:\s+(?:run|exec|--?[A-Za-z0-9_.=:/-]+))*"
    r"\s+test(?:\s|$)"
)
_ORIGINAL_TEST_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:(?:uv|poetry|pipenv)\s+run\s+)?python(?:3(?:\.\d+)?)?"
    r"(?:\s+--?[A-Za-z0-9_.=:/-]+)*"
    r"\s+tests/[^\s;&|]*"
)

_TEST_COMMAND_INPUTS = [
    # positives
    "npm test",
    "pnpm run test",
    "yarn exec test",
    "uv run --frozen test",
    "cargo  test",
    "make test",
    "npm --foo test",
    "go run --x --y test",
    "poetry run --no-root test ",
    # negatives
    "npm testing",
    "npm run build",
    "gotest",
    "pytest",
    "mytest",
    "xnpm test",
    "npm--test",
    "npm run --frozen build",
]

_TEST_PATH_INPUTS = [
    # positives
    "python tests/x.py",
    "uv run python3.12 tests/unit",
    "python -q tests/a",
    "python3 --foo tests/unit/x",
    "poetry run python tests/",
    # negatives
    "python tests",
    "pythontests/x",
    "python testsuite",
    "python -q tests",
    "xpython tests/a",
]


def _attr(module: ModuleType, name: str) -> re.Pattern[str]:
    pattern = getattr(module, name)
    assert isinstance(pattern, re.Pattern)
    return pattern


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
@pytest.mark.parametrize("text", _TEST_COMMAND_INPUTS)
def test_test_command_re_matches_original(module: ModuleType, text: str) -> None:
    """Hardened ``_VALIDATION_TEST_COMMAND_RE`` keeps the original match/span."""
    expected = _ORIGINAL_TEST_COMMAND_RE.search(text)
    actual = _attr(module, "_VALIDATION_TEST_COMMAND_RE").search(text)
    assert (actual is None) == (expected is None)
    if expected is not None and actual is not None:
        assert actual.span() == expected.span()


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
@pytest.mark.parametrize("text", _TEST_PATH_INPUTS)
def test_test_path_re_matches_original(module: ModuleType, text: str) -> None:
    """Hardened ``_VALIDATION_TEST_PATH_RE`` keeps the original match/span."""
    expected = _ORIGINAL_TEST_PATH_RE.search(text)
    actual = _attr(module, "_VALIDATION_TEST_PATH_RE").search(text)
    assert (actual is None) == (expected is None)
    if expected is not None and actual is not None:
        assert actual.span() == expected.span()


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_test_command_re_no_catastrophic_backtracking(module: ModuleType) -> None:
    """A long flag run with no trailing ``test`` must reject in linear time.

    Regression for CodeQL ``py/redos`` — the original pattern blows up
    exponentially on this input; the possessive form rejects in ~microseconds.
    """
    pathological = "npm " + "--x " * 5000 + "z"
    pattern = _attr(module, "_VALIDATION_TEST_COMMAND_RE")
    start = time.perf_counter()
    assert pattern.search(pathological) is None
    assert time.perf_counter() - start < 1.0


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_test_path_re_no_catastrophic_backtracking(module: ModuleType) -> None:
    """A long flag run with no trailing ``tests/`` must reject in linear time.

    Regression for CodeQL ``py/polynomial-redos`` on the test-path detector.
    """
    pathological = "python " + "--x " * 5000 + "z"
    pattern = _attr(module, "_VALIDATION_TEST_PATH_RE")
    start = time.perf_counter()
    assert pattern.search(pathological) is None
    assert time.perf_counter() - start < 1.0
