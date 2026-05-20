# PRRT_kwDOSJAM6s6DdI_T GitHub Actions Informational Context Plan

## Problem Statement And Scope

The informational workflow classifier only allows `${{ github.sha }}` and
`${{ steps.<id>.outcome }}` inside safe `echo` or `printf` commands. That
creates false positives for low-risk GitHub Actions values commonly used in PR
comment summaries, such as run identifiers, PR numbers, step conclusions, and
step or job result outputs. The scope is limited to the GitHub Actions
expression allowlist used by informational run-command safety.

## Requirements Checklist

- Add regression coverage for informational comment output that includes safe
  GitHub run and PR metadata expressions.
- Add regression coverage for informational comment output that includes safe
  step and needs result/output expressions.
- Preserve existing blocks for secret-bearing or broadly unsafe expressions,
  including `secrets.*`, `github.token`, and sensitive `env.*` names.
- Keep the implementation scoped to the quality-gate classifier and its focused
  tests.

## Implementation Steps

1. Add failing unit cases for the missing safe expression contexts.
2. Expand the safe informational GitHub Actions expression classifier without
   allowing secret-bearing contexts.
3. Run the focused quality-gate test subset that covers allowed and blocked
   informational expressions.
4. Run lint/type checks for the touched Python files if feasible.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'github_actions_expression_echo or secret_bearing_expansions'`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
