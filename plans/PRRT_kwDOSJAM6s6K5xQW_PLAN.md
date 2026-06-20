# PRRT_kwDOSJAM6s6K5xQW Plan

## Problem Statement And Scope

The PR monitor repairs a shared mirror's `core.hooksPath` before pre-push validation, but the actual `git push` occurs later. Validation commands or a sibling workspace can poison the mirror again before publish. The fix must fail closed immediately before invoking `git push`.

Scope is limited to the PR monitor push path and focused regression coverage for the review thread.

## Requirements Checklist

- Verify whether the current code repairs `core.hooksPath` immediately before the actual push.
- Add a fail-closed mirror hooks-path repair at the push boundary before `git push`.
- Return the existing mirror-poisoning reason code without invoking `git push` if repair fails.
- Add focused regression coverage proving a poisoned mirror blocks before push.
- Avoid broad AWF/GitHub-owned validation; record focused checks only.

## Implementation Steps

1. Inspect `pre_push_validation._validated_git_push_result` and `remote_ops._git_push_result`.
2. Add the repair to `_git_push_result` just before its `git push` runner call.
3. Add a focused unit test in the remote push outcome tests.
4. Run the narrow targeted unit test file or selected test.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6K5xQW_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q`

Pass criteria: the new regression and existing focused remote push tests pass. Full AWF/GitHub validation is managed after agent completion.
