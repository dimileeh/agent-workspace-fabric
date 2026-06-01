# PR 348 Mixed Pre-Push Failure Details Plan

## Problem Statement And Scope

PR review comment `issue:4587587225` reports a missing end-to-end regression test for the mixed `returncode=127` plus real validation failure path when pre-push validation fix passes are exhausted. Existing coverage checks the fix-pass prompt path and commit-failure path, but not the final `_GitPushResult.details` payload returned after a successful fix commit followed by another mixed validation failure.

Scope is limited to targeted regression coverage for `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`. Production behavior should remain unchanged unless the new test exposes a defect.

## Requirements Checklist

- Add a regression test that queues a mixed 127/non-127 validation failure, allows the fix commit to succeed, then queues another failing validation result so max fix passes are exhausted.
- Assert the terminal push result stays `PRE_PUSH_VALIDATION_FAILED`.
- Assert `result.details["failing_command"]` and `result.details["failing_returncode"]` identify the non-127 validation failure.
- Assert the monitor does not attempt `git push` after exhausted pre-push validation.
- Run only focused validation for the changed test; leave broad AWF/GitHub validation to the AWF post-agent phase.

## Implementation Steps

1. Inspect the existing pre-push validation tests and helper fakes.
2. Add a focused mixed-failure exhaustion test beside related pre-push validation regression tests.
3. Run the narrow pytest selection for the new test, and optionally the adjacent mixed-failure regression if needed.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

Command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k "mixed_127"
```

Pass criteria:

- The targeted mixed-127 regression tests pass.
- No full-repository test suite, full coverage gate, frontend build, push, branch switch, or rebase is run in the agent phase.
