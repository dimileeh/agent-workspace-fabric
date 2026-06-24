# Merge Attention Clock Seam Validation

Plan reference: `plans/MERGE_ATTENTION_CLOCK_SEAM_PLAN.md`

## Requirement Status

- Add one runner now-provider seam: Complete.
  - Added `now` to `_RunnerDeps` and `PullRequestMonitorRunner`, defaulting to
    `datetime.now(UTC)` via `_utcnow`.

- Use the seam for merge critical-section entry, marker stamps, freshness
  checks, durable re-stamps, and workspace attention timestamps: Complete.
  - Updated `merge_loop.py` critical-section entry, merge-rejection marker
    stamping, and merge critical-section grace decoration.
  - Updated `merge_attention.py` attention writes, TTL reference fallback, and
    durable re-stamp time.

- Tighten both fresh-at-entry regressions with a small TTL and fake clock:
  Complete.
  - Added `_FakeClock` and deterministic `_LongWaitMergeCoordinator`.
  - Retightened the long coordinator wait and post-lock queue wait regressions to
    `merge_block_attention_ttl_seconds=1.0`.

- Add #677 regression for GitHub `CLEAN` preserving merge-rejection-origin
  attention: Complete.
  - Added
    `test_github_clean_status_preserves_merge_rejection_attention_during_queue_wait`.

- Preserve GitHub-vs-Bitbucket `CLEAN` asymmetry for non-rejection observable
  resolution: Complete.
  - Kept Bitbucket `CLEAN` preservation.
  - Added
    `test_github_clean_status_clears_non_rejection_attention_during_queue_wait`.

- Keep scope minimal: Complete.
  - Touched only merge-attention/merge-loop clock logic, runner dependency
    plumbing, focused tests, and required plan/validation docs.

## Evidence

Initial red check:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_merge_attention.py::test_long_merge_coordinator_wait_preserves_fresh_at_entry_attention \
  tests/unit/runtime/test_pr_monitor_merge_attention.py::test_long_coordinator_wait_preserves_fresh_at_entry_attention_across_post_lock_queue_wait \
  tests/unit/runtime/test_pr_monitor_merge_attention.py::test_github_clean_status_preserves_merge_rejection_attention_during_queue_wait -q
```

Result before implementation: failed as expected because `now=` was unsupported
and GitHub `CLEAN` cleared the merge-rejection marker.

Focused passing checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q
```

Result: `21 passed in 28.27s`.

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/pr_monitor_runner/merge_attention.py \
  src/awf/runtime/pr_monitor_runner/merge_loop.py \
  src/awf/runtime/pr_monitor_runner/runner.py \
  src/awf/runtime/pr_monitor_runner/types.py \
  tests/unit/runtime/test_pr_monitor_merge_attention.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/runtime/pr_monitor_runner/merge_attention.py \
  src/awf/runtime/pr_monitor_runner/merge_loop.py \
  src/awf/runtime/pr_monitor_runner/runner.py \
  src/awf/runtime/pr_monitor_runner/types.py
```

Result: passed.

Full AWF/GitHub validation, full coverage, and CI-equivalent suites were not run
inside the agent phase per the workspace contract.
