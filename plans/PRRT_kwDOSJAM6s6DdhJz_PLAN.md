# PRRT_kwDOSJAM6s6DdhJz Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6DdhJz` reports that unowned informational
workflow steps may print secret-derived GitHub Actions data through
innocuous-looking `env.*` or `steps/needs.*.outputs.*` expressions. The quality
gate currently treats those expressions as safe unless their identifier names
look sensitive, which is not a reliable provenance check.

Scope is limited to workflow informational/comment safety in
`src/awf/control/quality_gates.py` and focused regression tests in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Block `env.*` expressions in informational run commands and comment action
  inputs, regardless of identifier name.
- Block `steps.*.outputs.*` and `needs.*.outputs.*` expressions in
  informational run commands and comment action inputs, regardless of output
  name.
- Preserve existing safe fixed metadata expressions such as `github.sha`,
  pull request number, `steps.*.outcome`, `steps.*.conclusion`, and
  `needs.*.result`.
- Add regression coverage for innocuous-looking data-bearing expression names.
- Keep changes scoped and do not alter branch or push behavior.

## Implementation Steps

1. Add/update failing tests that demonstrate innocuous `env.*` and
   `steps/needs.*.outputs.*` expressions are rejected.
2. Tighten the GitHub Actions expression allowlist in the quality gate.
3. Remove any now-unnecessary name-based allow logic for these expressions.
4. Run the targeted unit tests, then the relevant lint/type checks if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  must pass.
