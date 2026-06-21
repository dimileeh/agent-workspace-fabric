# PRRT_kwDOSJAM6s6K0BbW Plan

## Problem Statement and Scope

The review thread reports that missing-HEAD filesystem recovery can run while a
sync-base merge is still in progress. The helper resets the index with
`git reset --mixed HEAD`, which clears merge state such as `MERGE_HEAD`; the
following recovery commit can then become a normal single-parent commit instead
of preserving the intended base-merge parent.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_repair.py` and a
focused regression test for this helper.

## Requirements

- [ ] Detect an in-progress merge before the missing-HEAD recovery helper runs
  the reset that would clear merge state.
- [ ] Fail closed in that state by returning no recovered HEAD, allowing existing
  missing-HEAD error handling to block the push.
- [ ] Preserve the existing non-merge recovery path.
- [ ] Add a focused regression test proving no reset or commit runs when
  `MERGE_HEAD` is present.

## Implementation Steps

1. Add a narrow merge-state probe in `_recover_missing_head_object_from_filesystem`
   after branch-ref validation and before `update-ref` / `reset --mixed HEAD`.
2. If `MERGE_HEAD` is present, log the fail-closed reason and return `None`.
3. Update existing fake-runner tests for the added non-merge probe where needed.
4. Add a focused unit test for the merge-in-progress guard.

## Verification

Run only focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k "recover_missing_head_object"
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py plans/PRRT_kwDOSJAM6s6K0BbW_PLAN.md
```

Full AWF/GitHub validation is managed by AWF after this agent phase.
