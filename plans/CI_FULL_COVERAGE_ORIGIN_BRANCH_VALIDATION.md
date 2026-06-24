# CI Full Coverage Origin Branch Validation

Plan reference: `plans/CI_FULL_COVERAGE_ORIGIN_BRANCH_PLAN.md`

## Requirement Status

- Complete: Add meaningful regression tests for merge-block attention origin
  behavior that was uncovered by the full coverage report.
- Complete: Cover persisted merge-rejection origin. The new restart-style test
  verifies GitHub `CLEAN` queue waits preserve attention when the row carries
  merge-rejection origin.
- Complete: Cover persisted non-merge origin. The new test verifies GitHub
  `CLEAN` clears ordinary attention and removes both marker and origin keys.
- Complete: Cover explicit in-memory non-merge origin precedence. The new test
  verifies current state clears even when the row still contains older
  merge-rejection origin.
- Complete: Run focused local verification only. No broad coverage gate or full
  AWF validation suite was run inside the agent phase.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`
- `plans/CI_FULL_COVERAGE_ORIGIN_BRANCH_PLAN.md`
- `plans/CI_FULL_COVERAGE_ORIGIN_BRANCH_VALIDATION.md`

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py::test_queue_wait_preserves_persisted_merge_rejection_origin_after_restart tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py::test_queue_wait_clears_persisted_non_rejection_origin_when_github_clean tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py::test_queue_wait_uses_in_memory_non_rejection_origin_before_persisted_origin -q
```

Result: `3 passed in 4.21s`

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev ruff format tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py
```

Result: `1 file reformatted`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py -q
```

Result after formatting: `15 passed in 16.88s`

Full AWF/GitHub validation, including the exact `python-full-coverage` gate, is
managed after agent completion per the workspace contract.
