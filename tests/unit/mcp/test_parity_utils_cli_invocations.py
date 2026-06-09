from __future__ import annotations

import pytest

from tests.unit.mcp._parity_utils import _extract_cli_invocations_from_cell


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        # Empty / explicit-absence cells yield no invocations.
        ("", []),
        ("   ", []),
        ("CLI absent", []),
        ("`CLI absent`", []),
        # Bare command with no flags or placeholders.
        ("`awf init`", [("awf init", ())]),
        # The `<placeholder>` token terminates the base command exactly like a
        # `--flag` does, so the angle-bracket argument never leaks into command.
        ("`awf init <path>`", [("awf init", ())]),
        # A flag after the placeholder is still captured in flags.
        ("`awf init <path> --template <type>`", [("awf init", ("--template",))]),
        # A `--flag` terminator (no placeholder) behaves the same way.
        ("`awf workspace create --profile auto`", [("awf workspace create", ("--profile",))]),
        # `--flag=value` is normalized to the flag name only.
        ("`awf workspace create --profile=auto`", [("awf workspace create", ("--profile",))]),
        # A placeholder appearing before any flag still stops the command, and
        # later flags are captured even though a bare placeholder sits in front.
        (
            "`awf workspace show <id> --format json`",
            [("awf workspace show", ("--format",))],
        ),
        # Comma/semicolon-separated cells split into independent invocations,
        # each parsed with its own `<placeholder>` boundary.
        (
            "`awf init <path>`, `awf workspace show <id>`",
            [("awf init", ()), ("awf workspace show", ())],
        ),
    ],
)
def test_extract_cli_invocations_placeholder_boundary(
    cell: str, expected: list[tuple[str, tuple[str, ...]]]
) -> None:
    invocations = _extract_cli_invocations_from_cell(cell)
    assert [(inv.command, inv.flags) for inv in invocations] == expected
