# Plan: PRRT_kwDOSJAM6s6Kq_8T — fail closed when no-commit-clean HEAD cannot be compared

## Problem statement and scope

Review thread `PRRT_kwDOSJAM6s6Kq_8T` on PR #615 points at
`src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py:621`
(the no-commit-clean self-commit re-validation gate).

The current gate only runs the committed-delta ownership check when BOTH
`finalize_start_head` and `post_agent_head` are present AND differ:

```python
if (
    finalize_start_head is not None
    and post_agent_head is not None
    and post_agent_head != finalize_start_head
):
    post_no_commit_delta = await _committed_delta_paths(...)
```

Defect: if either anchor transiently fails to resolve (`_rev_parse_head`
returns `None`), HEAD movement cannot be ruled out, yet the gate is skipped,
the clean recheck is accepted, the caller recaptures HEAD (which may succeed
on retry), and an uninspected agent self-commit can be pushed without
comparing its committed delta to `owned_delta_paths`.

At the no-commit-clean branch both `operation_start_head` and
`owned_delta_paths` are guaranteed non-None (early returns at lines 333 and
340), so the delta check can always run.

## Requirements checklist

1. Run the committed-delta ownership check whenever HEAD movement cannot be
   *proven* absent — i.e. unless both anchors are present AND equal.
2. When a missing anchor prevents the comparison AND the committed delta
   cannot be inspected, fail closed with
   `_PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON` (mirroring the
   `committed=True` path).
3. When a missing anchor prevents the comparison AND the committed delta
   contains paths outside `owned_delta_paths`, fail closed with
   `_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON`.
4. Preserve existing behavior: when both anchors are present and equal (no
   self-commit), the gate is skipped and the clean recheck proceeds.
5. Preserve existing behavior: when both anchors are present and differ, the
   delta check runs exactly as today.
6. Add regression tests for the two new fail-closed paths
   (finalize_start_head missing; post_agent_head missing).

## Implementation steps

1. Write failing regression tests in
   `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_delta.py`
   for:
   - finalize_start_head is None (initial rev-parse failed) + unowned
     self-commit delta → `PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON`.
   - post_agent_head is None (transient rev-parse failure) + unowned
     self-commit delta → `PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON`.
2. Confirm the new tests fail against the current code.
3. Restructure the gate condition in
   `pre_push_validation_dirty_finalize.py` to run the delta check whenever
   HEAD movement is unknown (`finalize_start_head is None or post_agent_head
   is None or post_agent_head != finalize_start_head`).
4. Update the explanatory comment to record the new fail-closed behavior and
   the review thread id.
5. Re-run the targeted tests + existing no-commit-clean tests to confirm green.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_delta.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_edges.py -q` — all green.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_delta.py` — clean.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py` — clean.

Broad AWF/GitHub validation (full coverage gate, whole-repo suite) is owned by
AWF after agent completion and is not run here.
