# PR 348 Mixed Pre-Push Failure Details Validation

Plan reference: `plans/PR_348_MIXED_PRE_PUSH_FAILURE_DETAILS_PLAN.md`

## Requirement Status

- Complete: Added a regression test that queues a mixed 127/non-127 validation failure, allows the fix commit to succeed, then queues another mixed failure so max fix passes are exhausted.
- Complete: The new test asserts the terminal push result stays `PRE_PUSH_VALIDATION_FAILED`.
- Complete: The new test asserts `result.details["failing_command"] == "pytest -q"` and `result.details["failing_returncode"] == 1`, proving the non-127 validation failure drives terminal diagnostics.
- Complete: The new test asserts `git push` is not attempted after exhausted pre-push validation.
- Complete: Validation used focused checks only; full AWF/GitHub validation remains owned by the AWF post-agent phase.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `plans/PR_348_MIXED_PRE_PUSH_FAILURE_DETAILS_PLAN.md`
- `plans/PR_348_MIXED_PRE_PUSH_FAILURE_DETAILS_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k "mixed_127"
```

Result: `3 passed, 21 deselected in 4.00s`

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_pre_push_validation.py
```

Result: `All checks passed!`

No broad validation suite, coverage gate, frontend build, push, branch switch, or rebase was run in the agent phase.
