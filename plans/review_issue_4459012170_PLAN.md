# Review Issue 4459012170 Plan

## Problem Statement and Scope

Greptile reported that `src/awf/cli/main.py` calls `_configure_rich_help_width()` at import time, permanently mutating `typer.rich_utils.MAX_WIDTH` for every Typer command in the process. The fix should preserve the `workspace adopt-pr --help` visibility regression for `--model` and `--effort` on narrow terminals without leaking the width override globally.

## Requirements Checklist

- Remove the unconditional import-time mutation of `typer.rich_utils.MAX_WIDTH`.
- Preserve `workspace adopt-pr --help` output that exposes `--model` and `--effort` when the Rich help width is narrow.
- Scope any Rich width override to the relevant help render and restore the previous value afterward.
- Add or update regression tests proving the scoped behavior.
- Run the narrow CLI unit tests that cover this review issue.

## Implementation Steps

1. Add a small Typer command subclass or equivalent helper that temporarily raises Rich help width to at least 80 columns during `workspace adopt-pr` help formatting.
2. Apply the scoped help formatter only to `workspace adopt-pr`.
3. Update CLI tests to assert the flags remain visible and `typer.rich_utils.MAX_WIDTH` is restored after help rendering.
4. Run the focused CLI tests for `workspace adopt-pr`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q -k adopt_pr`
  - Passes with all selected tests green.
