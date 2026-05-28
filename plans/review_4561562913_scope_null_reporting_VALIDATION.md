# Review 4561562913 Scope Null Reporting Validation

Plan reference:
`plans/review_4561562913_scope_null_reporting_PLAN.md`

## Requirement Status

- Complete: Preserved default `provider=env` and `kind=env` only when those
  fields are omitted.
- Complete: Preserved rejection of explicit unsupported, null, and falsy
  `provider` or `kind` values.
- Complete: Added focused regression coverage proving explicit `null` is
  reported as `None`, not the string `"None"`.
- Complete: Kept checks focused to the touched unit-test surface.
- Complete: Documented validation evidence in this file.

## Files Changed

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`
- `plans/review_4561562913_scope_null_reporting_PLAN.md`
- `plans/review_4561562913_scope_null_reporting_VALIDATION.md`

## Review Note Resolution

The suggested `or "env"` fallback would weaken existing regression coverage by
normalizing explicit null and falsy scope values to `env`. This implementation
instead separates omitted fields from explicit values, keeps explicit null
invalid, and avoids the previous `str(None)` diagnostic.

The PyYAML formatting note in the same review-level comment did not require a
new change: `_refresh_optional_companion_env_secrets_for_resume` already returns
without writing when `removed_count == 0 and restored_count == 0`, and existing
resume-refresh tests cover list-style preservation for same-pass removal and
restore.

## Evidence

Initial failing regression check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_reports_null_environment_secret_scope_field_without_stringifying_none -q
```

Result: failed as expected because the error message contained
`provider='None'`.

Final focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_reports_null_environment_secret_scope_field_without_stringifying_none -q
```

Result: 1 passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result: 57 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py
```

Result: all checks passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py
```

Result: success, no issues found in 1 source file.

Full AWF/GitHub validation was not run during the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
