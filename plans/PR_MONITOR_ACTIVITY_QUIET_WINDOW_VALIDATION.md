# PR Monitor Activity Quiet Window Validation

## Outcome

Implemented the activity-based `NON_CHECK_REVIEWER_SETTLE` quiet window.

The monitor now computes a quiet anchor from external review activity using
GitHub `updatedAt` timestamps where available. When no external review activity
exists, it falls back to pull request/head creation activity. Long-running CI can
therefore satisfy the quiet window before checks turn green, while new review
activity on the same head resets the clock.

## Requirements Check

- Activity is based on review-thread comments, reviews, and top-level PR
  feedback before unresolved-only filtering: done.
- Resolved review-thread comments still count for the quiet clock: done.
- Viewer/AWF-authored feedback is excluded from external review activity:
  done.
- No-comment PRs fall back to pull request/head activity: done.
- Existing `monitor.non_check_reviewer_settle_seconds` config and
  `NON_CHECK_REVIEWER_SETTLE` reason code are preserved: done.
- Settled markers include the activity timestamp/source signature so new
  activity on the same head invalidates old quiet-window completion: done.
- Operator payloads include activity anchor, source, quiet-until, remaining
  seconds, and latest external activity details: done.
- Manual/no-auto-merge human-ready notification uses the same quiet clock:
  done.
- The separate `pre_merge_settle_seconds` race guard was not changed: done.

## Focused Validation

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q
```

Result: `170 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_pr_monitor_manual_merge.py -q
```

Result: `115 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py tests/unit/runtime/_monitor_runner_fixtures.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py
```

Result: passed.

## Notes

Validation stayed intentionally focused. Full coverage and whole-repository
validation remain the responsibility of AWF/GitHub CI for this slice.
