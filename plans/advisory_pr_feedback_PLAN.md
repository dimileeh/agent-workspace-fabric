# Advisory PR Feedback Plan

## Problem Statement And Scope

AWF currently treats advisory top-level PR feedback and review-state blockers as the same merge gate. This can park auto-merge at `NotifyHuman` when GitHub has no effective `CHANGES_REQUESTED` review, especially for bot `COMMENTED` reviews and top-level issue comments.

This implementation separates the full feedback list used by the address-comments loop from the merge-gate-only review blocker view. Scope is limited to GitHub PR status parsing, PR monitor decisions, PR monitor runner logging/helper checks, and focused unit tests.

## Requirements Checklist

- Preserve the complete `unresolved_review_comments` list for advisory feedback triage.
- Add `PRStatus.blocking_reviews`, populated only by effective `CHANGES_REQUESTED` review semantics.
- Set `ReviewComment.blocks_merge` only for review-level items that block by GitHub semantics.
- Keep unresolved inline review-thread gating unchanged.
- Use `blocking_reviews` for review blockers in merge decisions and runner helper checks.
- Allow `BLOCKED` / `HAS_HOOKS` to reach the normal merge attempt path when all other gates are green and `blocking_reviews` is empty.
- Emit both `unresolved_reviews` and `blocking_reviews` in monitor action and pre-merge recheck logs.
- Follow TDD with failing tests before implementation and run the requested validation commands.

## Implementation Steps

1. Add focused tests in `tests/unit/common/test_github_client.py`, `tests/unit/runtime/test_pr_monitor.py`, and `tests/unit/runtime/test_monitor_action_logging.py`.
2. Add `blocking_reviews` to `PRStatus` with an empty tuple default.
3. Update GitHub review parsing to build an effective latest-review-per-author map and expose only effective `CHANGES_REQUESTED` reviews as blockers.
4. Refactor `decide()` to consult `blocking_reviews`, preserve advisory comment addressing, and relax `BLOCKED` / `HAS_HOOKS` only when no other merge gate needs human attention.
5. Update runner logging and helper functions to emit/use `blocking_reviews`.
6. Run targeted tests, then ruff and mypy.
7. Write `plans/advisory_pr_feedback_VALIDATION.md` with requirement status and command evidence.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_monitor_action_logging.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_monitor_action_logging.py
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: all commands complete successfully, and the validation artifact records each requirement as complete or explains any remaining gap.
