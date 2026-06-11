"""ReDoS hardening regressions for the setup-status next-step command pattern.

CodeQL flagged the ``start_source`` value sub-pattern in
``_SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN`` (``setup_tools.py:124``) for
catastrophic backtracking: the ``(?:'[^']*'|"[^"]*"|\\S)+?`` value lets the
``\\S`` branch overlap the quoted-string branches (a quote is also ``\\S``), so
a quote-heavy run has Catalan-many tokenizations.

The hardened pattern makes the three branches a unique partition (atomic quoted
strings, non-quote chars, and a lone-quote branch reachable only when it does
*not* open a complete quoted string). This preserves the match on the realistic
AWF-generated next-step inputs the pattern actually sees (paths, optionally
quoted, with the documented suffix forms) and removes the backtracking.
"""

from __future__ import annotations

import re
import time

import pytest

from awf.mcp.setup_tools import (
    _SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN,
    _setup_status_next_step_for_source_checkout,
)

# Embedded copy of the ORIGINAL (pre-hardening) full pattern, kept local to the
# test so the differential guard proves equivalence without re-shipping the
# vulnerable ``\S``-overlap value sub-pattern.
_ORIGINAL_NEXT_STEP_PATTERN = re.compile(
    r"(?P<setup>\bawf setup --dry-run(?P<setup_suffix>[.,;):]|$|\s+to\b))"
    r"|(?P<start_source>\bawf start\s+--source-checkout(?:=|\s+)"
    r"(?:'[^']*'|\"[^\"]*\"|\S)+?"
    r"(?P<start_source_suffix>[.,;):](?=\s|$)|$|\s+to\b))"
    r"|(?P<start>\bawf start(?P<start_suffix>[.,;):]|$|\s+to\b))"
)

# Representative next-step strings (the only shape this pattern sees in
# production: a single ``--source-checkout`` argument — bare or fully quoted —
# followed by one of the documented suffix forms).
_REPRESENTATIVE_NEXT_STEPS = [
    "Run awf start --source-checkout=/path/to/repo.",
    "Run awf start --source-checkout=/path/to/repo to deploy.",
    "awf start --source-checkout='/p with space/r'",
    'awf start --source-checkout="/p with space/r" to continue',
    "awf start --source-checkout=/p/r;",
    "awf start --source-checkout=/p/r)",
    "awf start --source-checkout=/p/r:",
    "awf start --source-checkout=/p/r",
    "awf start --source-checkout=it's/repo",  # unbalanced quote -> lone-quote branch
    "awf start --source-checkout /opt/aira.",
    "Then run awf setup --dry-run to provision.",
    "awf start to begin.",
    "awf start.",
    "no command here at all",
]


@pytest.mark.unit
@pytest.mark.parametrize("text", _REPRESENTATIVE_NEXT_STEPS)
def test_next_step_pattern_matches_original(text: str) -> None:
    """Hardened pattern keeps the original span + named groups on real inputs."""
    expected = _ORIGINAL_NEXT_STEP_PATTERN.search(text)
    actual = _SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN.search(text)
    assert (actual is None) == (expected is None)
    if expected is not None and actual is not None:
        assert actual.span() == expected.span()
        assert actual.groupdict() == expected.groupdict()


@pytest.mark.unit
@pytest.mark.parametrize(
    "step, start_command, expected",
    [
        (
            "Run awf start --source-checkout=/old/path to deploy.",
            "awf start --source-checkout=/new/repo",
            "Run awf start --source-checkout=/new/repo to deploy.",
        ),
        (
            "awf start --source-checkout='/old path/r'.",
            "awf start --source-checkout=/new/repo",
            "awf start --source-checkout=/new/repo.",
        ),
        (
            "awf start --source-checkout=/old/r",
            "awf start --source-checkout=/new/repo",
            "awf start --source-checkout=/new/repo",
        ),
    ],
)
def test_next_step_rewrite_substitutes_command(
    step: str, start_command: str, expected: str
) -> None:
    """The hardened pattern drives ``.sub`` rewrites identically to before."""
    result = _setup_status_next_step_for_source_checkout(
        step,
        setup_command="awf setup --dry-run",
        start_command=start_command,
    )
    assert result == expected


@pytest.mark.unit
def test_next_step_pattern_no_catastrophic_backtracking() -> None:
    """A long quote run must not trigger exponential backtracking.

    Regression for CodeQL ``py/redos`` on ``setup_tools.py:124``. A long quote
    run followed by a non-suffix boundary forces the original ``\\S``-overlap
    value to explore Catalan-many tokenizations (exponential); the partitioned
    form completes in microseconds. The 5.0s budget sits far above any realistic
    parse time (so a loaded CI runner won't fail spuriously) yet far below the
    seconds-to-minutes a re-introduced regression would take.
    """
    # Trailing ``" x"`` denies any valid suffix, so the original pattern would
    # backtrack across every tokenization of the quote run before failing.
    pathological = "awf start --source-checkout=" + "'" * 5000 + " x"
    start = time.perf_counter()
    assert _SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN.search(pathological) is None
    assert time.perf_counter() - start < 5.0
