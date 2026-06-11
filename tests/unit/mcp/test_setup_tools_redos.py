"""ReDoS hardening regressions for the setup-status next-step command pattern.

CodeQL flagged the ``start_source`` value sub-pattern in
``_SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN`` (``setup_tools.py:124``) for
catastrophic backtracking: the ``(?:'[^']*'|"[^"]*"|\\S)+?`` value let the
``\\S`` branch overlap the quoted-string branches (a quote is also ``\\S``), so
a quote-heavy run had Catalan-many tokenizations.

The hardened pattern makes the three branches a unique partition (atomic quoted
strings, non-quote chars, and a lone-quote branch reachable only when it does
*not* open a complete quoted string). This preserves the match on the realistic
AWF-generated next-step inputs the pattern actually sees (paths, optionally
quoted, with the documented suffix forms) and removes the backtracking.

The expected span + named groups below were captured once from the pre-hardening
pattern. We bake them as literals rather than re-compiling the original
(vulnerable) regex here: re-shipping that literal would itself be a ``py/redos``
finding, and the differential guard is just as strong asserting the hardened
pattern against the captured span/groups.
"""

from __future__ import annotations

import time

import pytest

from awf.mcp.setup_tools import (
    _SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN,
    _setup_status_next_step_for_source_checkout,
)

# (input, expected span, expected groupdict) captured from the ORIGINAL
# pre-hardening ``_SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN``. ``None`` span == no
# match. The shapes are the only ones this pattern sees in production: a single
# ``--source-checkout`` argument — bare or fully quoted — followed by one of the
# documented suffix forms.
_NEXT_STEP_CASES: list[tuple[str, tuple[int, int] | None, dict[str, str | None]]] = [
    (
        "Run awf start --source-checkout=/path/to/repo.",
        (4, 46),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=/path/to/repo.",
            "start_source_suffix": ".",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "Run awf start --source-checkout=/path/to/repo to deploy.",
        (4, 48),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=/path/to/repo to",
            "start_source_suffix": " to",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start --source-checkout='/p with space/r'",
        (0, 45),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout='/p with space/r'",
            "start_source_suffix": "",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        'awf start --source-checkout="/p with space/r" to continue',
        (0, 48),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": 'awf start --source-checkout="/p with space/r" to',
            "start_source_suffix": " to",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start --source-checkout=/p/r;",
        (0, 33),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=/p/r;",
            "start_source_suffix": ";",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start --source-checkout=/p/r)",
        (0, 33),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=/p/r)",
            "start_source_suffix": ")",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start --source-checkout=/p/r:",
        (0, 33),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=/p/r:",
            "start_source_suffix": ":",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start --source-checkout=/p/r",
        (0, 32),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=/p/r",
            "start_source_suffix": "",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        # unbalanced quote -> lone-quote branch
        "awf start --source-checkout=it's/repo",
        (0, 37),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout=it's/repo",
            "start_source_suffix": "",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        # shlex.join() escapes an apostrophe path as adjacent quoted segments
        # ('it' + "'" + 's/repo'); the partitioned value must tokenize the run as
        # a series of atomic quoted strings rather than a single one.
        "awf start --source-checkout='it'\"'\"'s/repo'",
        (0, 43),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout='it'\"'\"'s/repo'",
            "start_source_suffix": "",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start --source-checkout /opt/aira.",
        (0, 38),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": "awf start --source-checkout /opt/aira.",
            "start_source_suffix": ".",
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "Then run awf setup --dry-run to provision.",
        (9, 31),
        {
            "setup": "awf setup --dry-run to",
            "setup_suffix": " to",
            "start_source": None,
            "start_source_suffix": None,
            "start": None,
            "start_suffix": None,
        },
    ),
    (
        "awf start to begin.",
        (0, 12),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": None,
            "start_source_suffix": None,
            "start": "awf start to",
            "start_suffix": " to",
        },
    ),
    (
        "awf start.",
        (0, 10),
        {
            "setup": None,
            "setup_suffix": None,
            "start_source": None,
            "start_source_suffix": None,
            "start": "awf start.",
            "start_suffix": ".",
        },
    ),
    ("no command here at all", None, {}),
]


@pytest.mark.unit
@pytest.mark.parametrize("text, expected_span, expected_groups", _NEXT_STEP_CASES)
def test_next_step_pattern_matches_original(
    text: str,
    expected_span: tuple[int, int] | None,
    expected_groups: dict[str, str | None],
) -> None:
    """Hardened pattern keeps the original span + named groups on real inputs."""
    actual = _SETUP_STATUS_NEXT_STEP_COMMAND_PATTERN.search(text)
    assert (actual is None) == (expected_span is None)
    if actual is not None and expected_span is not None:
        assert actual.span() == expected_span
        assert actual.groupdict() == expected_groups


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
        (
            # shlex.join()-escaped apostrophe path (adjacent quoted segments) as
            # the step's source-checkout token must rewrite as a single unit.
            "awf start --source-checkout='/old'\"'\"'s/r'.",
            "awf start --source-checkout=/new/repo",
            "awf start --source-checkout=/new/repo.",
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
    run followed by a non-suffix boundary forced the original ``\\S``-overlap
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
