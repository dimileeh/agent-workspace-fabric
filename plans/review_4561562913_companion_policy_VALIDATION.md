# Review 4561562913 Companion Policy Validation

Plan reference: `plans/review_4561562913_companion_policy_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add a regression test showing float `compose_up_timeout_seconds` values are handled explicitly during task-policy deserialization. | Complete | Added fractional and integral float timeout coverage in `tests/unit/node/test_companion_services.py`. The fractional test failed before implementation because no warning was logged. |
| Add a regression test showing invalid `environment_secrets` target keys are rejected during task-policy deserialization. | Complete | Added `test_companion_specs_from_task_policy_rejects_invalid_environment_secret_target` in `tests/unit/node/test_companion_services.py`. The test failed before implementation because no `ValueError` was raised. |
| Preserve existing API validation behavior and existing environment-secret resolution behavior. | Complete | Runtime change is isolated to `src/awf/node/companion_services.py`; existing focused companion-service tests continue to pass. |
| Keep checks focused to the changed unit-test surface. | Complete | Ran only the companion-service unit file and a narrow ruff check on edited Python files. Full AWF/GitHub validation remains managed by AWF after agent completion. |

## Verification Evidence

Failed-first check:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result before implementation: failed with two expected regression failures.

Final focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result: `34 passed in 0.57s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py
```

Result: `All checks passed!`.

## Remaining Gaps

None for the scoped review comment. Full AWF/GitHub validation is intentionally
left to AWF after agent completion per the workspace contract.
