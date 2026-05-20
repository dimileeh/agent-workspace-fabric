# PRRT_kwDOSJAM6s6Dckhk Validation

Plan reference: `PRRT_kwDOSJAM6s6Dckhk_PLAN.md`

## Requirement Status

- Complete: Branch open-PR lookup failures are distinct from successful
  no-match results.
  Evidence: `_resolve_preserved_active_branch_open_pr` now returns a
  `_BranchOpenPRLookup` with `state="failed"` and safe failure metadata when
  the resolver raises.
- Complete: Existing committed-work fallback remains intact.
  Evidence:
  `test_preserved_active_pushed_branch_pr_lookup_failure_falls_back_to_worktree_salvage`
  still passes and continues to request validation salvage.
- Complete: Lookup-failed + clean/not-ahead no longer creates a replacement.
  Evidence:
  `test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_is_operator_recoverable`
  asserts only the original workspace remains and replacement events are absent.
- Complete: The failed-lookup + clean/not-ahead case records operator recovery
  with non-secret branch lookup context.
  Evidence: the new regression asserts `ambiguity_reason ==
  "open_pr_lookup_failed"` and `branch_pr_lookup` contains only branch name,
  error type, failure category, and source.
- Complete: Genuine no-match lookup behavior is preserved.
  Evidence:
  `test_preserved_active_without_usable_work_creates_one_replacement_with_lineage`
  still passes.

## Validation Commands

- Initial failing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_is_operator_recoverable -q`
  failed before implementation because a replacement workspace was created.
- Focused passing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_is_operator_recoverable -q`
  passed.
- Focused adjacent coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_with_no_local_work_is_operator_recoverable tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_failure_falls_back_to_worktree_salvage tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_without_usable_work_creates_one_replacement_with_lineage tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_pushed_branch_pr_lookup_ambiguity_is_operator_recoverable -q`
  passed with 4 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  passed.
- Broader worker suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed with 201 tests.

## Remaining Gaps

None.
