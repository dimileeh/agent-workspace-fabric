# Review 4620252998 Compose GC Plan

## Problem Statement And Scope

Address the PR review-level comment for compose teardown behavior in completed
workspace filesystem GC. Scope is limited to the cited GC fallback allowlist and
monitor lifecycle observability paths.

## Requirements Checklist

- Verify whether retained completed workspaces with merged PRs can receive a
  fallback compose teardown when they are preserved by retention.
- Avoid broadening fallback compose teardown to unrelated failed/superseded
  retention-preserved workspaces.
- Ensure compose teardown outcomes are still logged when filesystem GC raises
  after the compose callback has already run.
- Distinguish missing compose project context (`None`) from an empty string so
  blank monitor fallback values do not silently skip teardown when candidate
  metadata is available.
- Cover behavior with focused regression tests only; full AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Update the preserved-workspace fallback predicate in `src/awf/service/gc.py`
   to allow `WORKSPACE_WITHIN_RETENTION` only for completed workspaces.
2. Update completed monitor compose teardown construction in
   `src/awf/runtime/pr_monitor_runner/lifecycle.py` to treat only `None` as
   missing project context, and track callback outcomes for exception logging.
3. Add targeted unit tests in existing GC/monitor completion test files for the
   reviewed cases.
4. Run only the focused tests that exercise the changed behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::<new-retained-merged-test> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::<new-monitor-tests> -q`

Pass criteria: all focused tests pass, and the validation document records that
full AWF/GitHub validation is intentionally left to AWF post-agent execution.
