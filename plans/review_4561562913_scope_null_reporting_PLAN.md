# Review 4561562913 Scope Null Reporting Plan

## Problem Statement And Scope

Review comment `issue:4561562913` flags that
`_environment_secret_scope_value` coerces explicit `null` task-policy
`provider` or `kind` values to the string `"None"`. The suggested fallback
would normalize `null`, `false`, and `0` to `env`, but existing regression
tests in `tests/unit/node/test_companion_services.py` treat explicit null and
falsy scope values as invalid task policy. This work preserves that validation
policy while making the absent-vs-explicit value handling clear and avoiding a
`None` to `"None"` diagnostic.

The resume YAML refresh formatting concern is already covered by the current
zero-change write guard and list-style preservation tests, so this plan limits
code changes to companion env-secret scope parsing.

## Requirements Checklist

- Preserve default `provider=env` and `kind=env` only when the fields are
  omitted.
- Preserve rejection of explicit unsupported, null, and falsy `provider` or
  `kind` values.
- Add focused regression coverage proving explicit `null` is reported as
  `None`, not the string `"None"`.
- Keep checks focused to the touched unit-test surface.
- Document validation evidence in
  `plans/review_4561562913_scope_null_reporting_VALIDATION.md`.

## Implementation Steps

1. Add a focused failing test for explicit `provider: null` error reporting.
2. Update `_environment_secret_scope_value` so omitted fields default to `env`
   and explicit `None` remains an invalid `None` value instead of becoming
   `"None"`.
3. Run targeted companion-service tests and a focused ruff check.
4. Record validation status and evidence.
5. Stage and commit only files changed for this review comment.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_reports_null_environment_secret_scope_field_without_stringifying_none -q
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py
uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py
```

Pass criteria: the new regression fails before implementation, then passes
with the companion-service focused unit surface, ruff, and focused mypy. Full
AWF/GitHub validation is intentionally left to AWF after agent completion.
