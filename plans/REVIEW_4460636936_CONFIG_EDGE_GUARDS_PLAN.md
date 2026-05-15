# Review 4460636936 Config Edge Guards Plan

## Problem Statement And Scope

Address review-level feedback for PR comment `issue:4460636936` on production
configuration guardrails. The scope is limited to two edge cases in
`src/awf/common/config.py`: blank production database URLs should fail with an
actionable guardrail diagnostic, and repeated weak API tokens with a trailing
separator should still be detected as weak.

## Requirements Checklist

- Preserve local and CI behavior; guardrails remain production-only.
- In production, reject empty and whitespace-only `database_url` values passed
  into `validate_production_settings`.
- Keep malformed database URL port behavior unchanged so existing parse errors
  still bubble.
- In production, reject repeated weak API token values even when the token ends
  with the same separator.
- Add focused regression coverage before implementation.
- Keep changes scoped to the guardrail helper and related tests.

## Implementation Steps

1. Update `tests/unit/service/test_config.py` with failing regressions for
   blank production database URLs and trailing-separator weak tokens.
2. Run the narrow tests to confirm the new regressions fail.
3. Update `src/awf/common/config.py` with the smallest helper changes.
4. Re-run the narrow tests and then lint the touched files.
5. Create `plans/REVIEW_4460636936_CONFIG_EDGE_GUARDS_VALIDATION.md` with
   requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/service/test_config.py
```

Pass criteria: the new tests fail before implementation, pass after the helper
changes, and ruff reports no issues for touched Python files.
