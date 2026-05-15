# PR 254 Colored Help CI Fix Plan

## Problem Statement And Scope

PR #254 still fails `python-full-coverage` on the two
`workspace adopt-pr --help` flag visibility tests. The focused tests pass in a
plain local terminal, but fail when GitHub Actions-style color is forced because
Rich inserts ANSI style sequences inside long option tokens. The CLI still
renders the flags, but the tests assert against raw colored output where
`--model` and `--effort` are split by escape codes.

Scope is limited to making the CLI help assertions color-aware while preserving
the existing narrow-terminal behavior and the `workspace adopt-pr` option
surface.

## Requirements Checklist

- Reproduce the reported pytest failures under a GitHub Actions-like colored
  terminal environment.
- Keep the `workspace adopt-pr --help` contract that exposes `--model` and
  `--effort`.
- Preserve the scoped Rich width override and restoration behavior.
- Add regression coverage for colorized Rich help output without disabling or
  weakening the check.
- Validate with the focused failing tests under the colored terminal
  environment and the adopt-pr CLI test slice.
- Record validation evidence in `plans/PR_254_COLORED_HELP_VALIDATION.md`.
- Commit the fix locally with a conventional `fix(ci): ...` message and do not
  push.

## Implementation Steps

1. Add a small test helper that strips ANSI escape sequences before checking
   rendered help text.
2. Update the existing adopt-pr help assertions to assert against normalized
   visible text.
3. Add a focused regression that forces Rich color in the test process and
   proves `--model` and `--effort` are visible after ANSI normalization.
4. Run the focused colored failure command, the adopt-pr CLI slice, and lint for
   touched files.

## Verification Commands And Pass Criteria

- `TERM=xterm-256color FORCE_COLOR=1 CLICOLOR_FORCE=1 GITHUB_ACTIONS=true CI=true uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_adopt_pr_help_exposes_model_and_effort_flags_when_terminal_is_narrow -q`
  - Passes, proving the CI failure mode is fixed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr -q`
  - Passes all adopt-pr CLI behavior tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py`
  - Passes lint for touched Python files.
