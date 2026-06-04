# PR403 Coverage Gate Fix Validation

## Result

Validated. The coverage failure was a real test gap in PR-touched runtime code,
not a reason to lower the global threshold.

## Evidence

GitHub `python-full-coverage` failed on run `26981800368` because combined
coverage was `98.97`, below the `99.00` fail-under. The downloaded
`full-coverage-report` artifact showed the largest PR-local gaps in:

- `src/awf/service/environment.py`
- `src/awf/service/env_migration.py`

The fix adds focused unit coverage for Compose env-file parsing/interpolation,
Compose include traversal, and legacy env migration formatting/append paths.
No broad coverage exclusion was added.

## Local Validation

Passed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py tests/unit/service/test_env_migration.py -q
# 44 passed in 0.42s

uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py src/awf/service/env_migration.py tests/unit/service/test_environment.py tests/unit/service/test_env_migration.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/service/environment.py src/awf/service/env_migration.py
# Success: no issues found in 2 source files
```

Focused coverage comparison against the failed CI artifact shows the new tests
recover 85 previously missing line/branch opportunities in the two affected
service files. That is comfortably larger than the 0.03 percentage-point
shortfall from the failing full-coverage job.

## Notes

One synthetic branch in `environment.py` was removed by replacing an
unnecessary `if key:` guard with a cast. The regex only matches interpolation
forms that include either the braced or plain variable group, so this does not
change runtime behavior.
