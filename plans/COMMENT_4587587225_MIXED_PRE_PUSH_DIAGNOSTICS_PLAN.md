# Comment 4587587225 Mixed Pre-Push Diagnostics Plan

## Problem Statement and Scope

PR review comment `issue:4587587225` reports missing diagnostic coverage for the PR
monitor pre-push validation terminal path when validation has both a command-not-found
failure (`returncode=127`) and a genuine validation failure. The existing implementation
should surface the genuine non-127 failure in `failure_details()`, even when a fix-pass
commit fails and the public reason becomes `PRE_PUSH_VALIDATION_FIX_FAILED`.

Scope is limited to regression coverage for this mixed-failure terminal path. Current
local code already caches failed-command collection and imports
`PR_MONITOR_SETUP_FAILED_REASON_CODE` from executor constants, so the summary remarks
about those areas are stale for this workspace.

## Requirements Checklist

- Add a focused unit test that scripts a mixed `ValidationResult` with one 127 failure
  and one non-127 failure.
- Drive the terminal fix-failed path by making the validation fix commit return false.
- Assert the returned details keep `PRE_PUSH_VALIDATION_FIX_FAILED` as the public
  reason while `validation_reason_code`, `failing_command`, and `failing_returncode`
  point at the non-127 validation failure.
- Avoid broad AWF/GitHub-owned validation; run only the targeted test needed for this
  regression.

## Implementation Steps

1. Add a test variant near the existing fix-pass commit failure regression in
   `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`.
2. Reuse the existing `_command_result`, `ValidationResult`, runner, and monkeypatch
   helpers to keep the test idiomatic.
3. Run the specific test with `uv run --python 3.12 --extra dev pytest ... -q`.
4. Record focused validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

Command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_mixed_127_fix_commit_failure_reports_real_pre_push_details -q
```

Pass criteria:

- The targeted regression test passes.
- No full-suite, coverage, push, branch switch, or CI-equivalent command is run during
  the agent phase.
