"""ReDoS hardening regressions for the ``quality_gates*`` validation regexes.

CodeQL flagged ``_VALIDATION_TEST_COMMAND_RE`` and ``_VALIDATION_TEST_PATH_RE``
(``py/redos`` / ``py/polynomial-redos``) in all five ``quality_gates*`` modules.
Both carried a repeated separator group whose greedy inner token class overlapped
an adjacent ``\\s+`` boundary, so a long run of flag-like tokens had exponentially
many ways to split → catastrophic backtracking.

The five modules carry byte-identical copies of these constants, so the same
accept-set + timing assertions run against every module's compiled pattern.

The expected ``search`` spans below were captured once from the pre-hardening
patterns. We bake the expected results as literals rather than re-compiling the
original (vulnerable) regexes here: re-shipping those literals would itself be a
``py/redos`` finding, and the differential guard is just as strong asserting the
hardened pattern against the captured spans.
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

# (input, expected search span) captured from the ORIGINAL pre-hardening
# ``_VALIDATION_TEST_COMMAND_RE``. ``None`` == no match.
_TEST_COMMAND_CASES: list[tuple[str, tuple[int, int] | None]] = [
    # positives
    ("npm test", (0, 8)),
    ("pnpm run test", (0, 13)),
    ("yarn exec test", (0, 14)),
    ("uv run --frozen test", (0, 20)),
    ("cargo  test", (0, 11)),
    ("make test", (0, 9)),
    ("npm --foo test", (0, 14)),
    ("go run --x --y test", (0, 19)),
    ("poetry run --no-root test ", (0, 26)),
    # negatives
    ("npm testing", None),
    ("npm run build", None),
    ("gotest", None),
    ("pytest", None),
    ("mytest", None),
    ("xnpm test", None),
    ("npm--test", None),
    ("npm run --frozen build", None),
]

# (input, expected search span) captured from the ORIGINAL pre-hardening
# ``_VALIDATION_TEST_PATH_RE``. ``None`` == no match.
_TEST_PATH_CASES: list[tuple[str, tuple[int, int] | None]] = [
    # positives
    ("python tests/x.py", (0, 17)),
    ("uv run python3.12 tests/unit", (0, 28)),
    ("python -q tests/a", (0, 17)),
    ("python3 --foo tests/unit/x", (0, 26)),
    ("poetry run python tests/", (0, 24)),
    # negatives
    ("python tests", None),
    ("pythontests/x", None),
    ("python testsuite", None),
    ("python -q tests", None),
    ("xpython tests/a", None),
]


def _attr(module: ModuleType, name: str) -> re.Pattern[str]:
    pattern = getattr(module, name)
    assert isinstance(pattern, re.Pattern)
    return pattern


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
@pytest.mark.parametrize("text, expected_span", _TEST_COMMAND_CASES)
def test_test_command_re_matches_original(
    module: ModuleType, text: str, expected_span: tuple[int, int] | None
) -> None:
    """Hardened ``_VALIDATION_TEST_COMMAND_RE`` keeps the original match/span."""
    actual = _attr(module, "_VALIDATION_TEST_COMMAND_RE").search(text)
    assert (actual is None) == (expected_span is None)
    if actual is not None and expected_span is not None:
        assert actual.span() == expected_span


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
@pytest.mark.parametrize("text, expected_span", _TEST_PATH_CASES)
def test_test_path_re_matches_original(
    module: ModuleType, text: str, expected_span: tuple[int, int] | None
) -> None:
    """Hardened ``_VALIDATION_TEST_PATH_RE`` keeps the original match/span."""
    actual = _attr(module, "_VALIDATION_TEST_PATH_RE").search(text)
    assert (actual is None) == (expected_span is None)
    if actual is not None and expected_span is not None:
        assert actual.span() == expected_span


@pytest.mark.unit
@pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_test_command_re_no_catastrophic_backtracking(module: ModuleType) -> None:
    """A long flag run with no trailing ``test`` must reject in linear time.

    Regression for CodeQL ``py/redos`` — the original pattern blew up
    exponentially on this input; the possessive form rejects in ~microseconds.
    """
    pathological = "npm " + "--x " * 5000 + "z"
    pattern = _attr(module, "_VALIDATION_TEST_COMMAND_RE")
    start = time.perf_counter()
    assert pattern.search(pathological) is None
    # 5.0s is far above the ~microsecond possessive-form reject and far below
    # the seconds-to-minutes a backtracking regression would take, so a loaded
    # CI runner won't fail spuriously.
    assert time.perf_counter() - start < 5.0


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
    # 5.0s budget: far above the possessive-form reject, far below a regression.
    assert time.perf_counter() - start < 5.0
