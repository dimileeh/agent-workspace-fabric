# PRRT_kwDOSJAM6s6Dcv0 GitHub Actions Secret Expressions Plan

## Problem Statement And Scope

The informational workflow run-command guard currently skips GitHub Actions
expression delimiters when looking for unsafe shell braced parameter expansion.
That preserves existing support for low-risk informational output such as
`${{ github.sha }}`, but it also lets secret-bearing expressions such as
`${{ secrets.GITHUB_TOKEN }}` pass through an otherwise unowned informational
`echo` or `printf` step. The scope is limited to informational run-command
safety in protected workflow diffs.

## Requirements Checklist

- Add regression coverage proving an informational workflow step blocks
  GitHub Actions expressions that can expose secrets.
- Preserve existing regression coverage that allows low-risk informational
  expressions such as `${{ github.sha }}` and `${{ steps.test.outcome }}`.
- Keep existing shell parameter protections for `${VAR}` and sensitive
  unbraced variables such as `$GH_TOKEN`.
- Keep the implementation scoped to `src/awf/control/quality_gates.py`, its
  focused unit tests, and the required plan/validation artifacts.

## Implementation Steps

1. Add failing unit coverage for secret-bearing GitHub Actions expressions in
   informational `run` commands.
2. Update informational parameter-expansion detection to classify unsafe
   GitHub Actions expressions before applying shell parameter checks.
3. Re-run the focused failing test to confirm it passes.
4. Run the relevant quality-gate unit test subset and static checks.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'secret_bearing_expansions or github_actions_expression_echo or informational_run_command_shell_safety_edges'`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
