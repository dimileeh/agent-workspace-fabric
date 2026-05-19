# Advisory PR Feedback Validation

Plan reference: `plans/advisory_pr_feedback_PLAN.md`

## Requirement Status

- Complete: Preserve full `unresolved_review_comments` for advisory feedback triage.
- Complete: Add `PRStatus.blocking_reviews` as a merge-gate-only view.
- Complete: Set `ReviewComment.blocks_merge` only for effective review-level `CHANGES_REQUESTED` blockers in GitHub parsing.
- Complete: Keep unresolved inline review-thread gating unchanged.
- Complete: Use `blocking_reviews` for review blockers in monitor decisions and runner helper checks.
- Complete: Relax `BLOCKED` / `HAS_HOOKS` escalation when CI, mergeability, inline threads, human defers, and blocking reviews are clean.
- Complete: Emit `blocking_reviews` beside `unresolved_reviews` in monitor action and pre-merge recheck logging.
- Complete: Add focused regression tests before implementation and validate with requested commands.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `src/awf/runtime/pr_monitor.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/common/test_github_client.py`
- `tests/unit/runtime/test_pr_monitor.py`
- `tests/unit/runtime/test_monitor_action_logging.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_monitor_action_logging.py -q
```

Result: `226 passed in 35.89s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_monitor_action_logging.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 157 source files`

Additional focused helper regression check:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::TestNotificationAndGraceHelpers::test_notify_human_reason_prioritizes_blocking_conditions tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_notify_human_reason_and_merge_rejection_detail tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_manual_ready_handoff_rejects_blocking_review_comments -q
```

Result: `3 passed in 3.23s`

Additional ruff coverage for adjacent helper tests touched to keep broader unit assertions aligned:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_monitor_action_logging.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py
```

Result: `All checks passed!`

## Gaps

No planned requirement is partial or missing.
