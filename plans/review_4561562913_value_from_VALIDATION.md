# Review 4561562913 Value From Validation

Plan reference: `plans/review_4561562913_value_from_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add a regression test showing invalid `environment_secrets.value_from` values are rejected during task-policy deserialization. | Complete | Added `test_companion_specs_from_task_policy_rejects_invalid_environment_secret_value_from` in `tests/unit/node/test_companion_services.py`. |
| Validate `value_from` with the same Docker-compatible environment variable name pattern used for environment-secret target keys. | Complete | `_environment_secret_ref` now validates `value_from` against `_ENVIRONMENT_KEY_PATTERN` before creating `CompanionEnvironmentSecretRef`. |
| Preserve existing valid `environment_secrets` deserialization behavior. | Complete | The companion-service unit file passes after the change. |
| Keep checks focused to the changed companion-service unit-test surface. | Complete | Ran only the focused regression, companion-service unit file, and ruff on edited Python files. Full AWF/GitHub validation remains managed by AWF after agent completion. |

## Verification Evidence

Failed-first check:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_rejects_invalid_environment_secret_value_from -q
```

Result before implementation: failed because the invalid `value_from` did not
raise `ValueError`.

Final focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_rejects_invalid_environment_secret_value_from -q
```

Result: `1 passed in 0.42s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result: `35 passed in 0.58s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py
```

Result: `All checks passed!`.

## Remaining Gaps

None for the scoped review comment. Full AWF/GitHub validation is intentionally
left to AWF after agent completion per the workspace contract.
