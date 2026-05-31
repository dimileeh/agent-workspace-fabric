# Review Comment Rollback Validation

Plan reference: `plans/REVIEW_COMMENT_ROLLBACK_PLAN.md`

## Requirement Status

- Regression for multi-pass review-comment rollback: Complete.
  Added
  `test_generic_push_failure_preserves_review_comment_needs_human_after_later_pass`
  in `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`.
- Preserve latest `needs_human` / `agent_failed` review-comment state across
  generic push-failure rollback: Complete.
  `src/awf/runtime/pr_monitor_runner/fix_cycle.py` now removes a re-addressed
  review comment from the stale publish-dependent rollback set when the latest
  verdict is `needs_human` or `agent_failed`.
- Keep rollback behavior for publish-dependent review-comment verdicts:
  Complete.
  `fix_committed` and `false_positive` still enter the publish-dependent set;
  `defer` remains excluded as before.
- Avoid broad AWF/GitHub-owned validation: Complete.
  Only focused unit and lint checks were run locally. Full AWF/GitHub validation
  remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/fix_cycle.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
- `plans/REVIEW_COMMENT_ROLLBACK_PLAN.md`
- `plans/REVIEW_COMMENT_ROLLBACK_VALIDATION.md`

Commands run:

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k review_comment_needs_human`
  failed with `KeyError: 'issue:4585067239'`, confirming the stale rollback set
  cleared the later `needs_human` verdict.
- Passing focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k review_comment_needs_human`
  passed.
- Passing nearby rollback checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k 'review_comment_needs_human or stores_needs_human or workflow_scope_push_failure_requeues or workflow_scope_requeue'`
  passed with 7 selected tests.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  passed.

## Gaps

No planned requirements remain partial or missing. Broad repository validation,
coverage, frontend builds, and CI-equivalent checks were intentionally not run
inside the agent phase per the AWF workspace contract.
