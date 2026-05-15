# Adopt PR Help CI Fix Plan

## Problem Statement and Scope

The CI `python-full-coverage` job fails on
`tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags`
because Typer/Rich abbreviates `workspace adopt-pr` option names when the
terminal width is narrow. Under `COLUMNS=30`, `--model` and `--effort` render as
truncated labels, so the help output no longer exposes the literal flags.

Scope is limited to making CLI help output stable enough for the affected flags
and adding a focused regression for narrow Rich help width behavior.

## Requirements Checklist

- Preserve `workspace adopt-pr --model` and `--effort` runtime behavior.
- Ensure `workspace adopt-pr --help` contains literal `--model` and `--effort`
  even under narrow CI terminal widths.
- Add or update focused regression coverage for the narrow-width help rendering
  issue.
- Do not disable, skip, or weaken the failing check.
- Validate with the focused failing pytest node under a narrow `COLUMNS` value.

## Implementation Steps

1. Configure Typer/Rich help rendering with a minimum help width at CLI import
   time.
2. Add a unit regression that simulates an undersized Rich help width and checks
   that the adopt-pr model and effort flags are still exposed.
3. Run the focused failing test normally and with `COLUMNS=30`.
4. Run the relevant CLI test module if the focused checks pass.

## Verification Commands and Pass Criteria

- `COLUMNS=30 uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags -q`
  - Passes and proves the reported CI failure mode is fixed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr -q`
  - Passes all adopt-pr CLI behavior tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py`
  - Passes lint for touched Python files.
