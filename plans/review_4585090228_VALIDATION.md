# Review 4585090228 Validation

Plan reference: `plans/review_4585090228_PLAN.md`

## Requirement Status

- Add/update regression coverage before behavior changes where practical: Complete
  - Added focused regressions for source-runtime blocked-event deduplication and cleanup candidate query shape.
- Dedupe repeated `PLANNING_SCOPE_AUTO_RETRY_SOURCE_RUNTIME_NOT_RELEASED` blocked events: Complete
  - `_record_planning_scope_auto_retry_blocked_after_retry_rollback` now dedupes any latest blocked event with the same reason code.
- Keep manual retry terminal semantics while narrowing cleanup candidate ranking: Complete
  - Cleanup now ranks only pending blocked/resume-failed markers and uses a newer terminal-event guard so later manual retry still suppresses auto-resume.
- Document `ignore_source_runtime_check=True` safety invariant: Complete
  - The service docstring now documents the bypass invariant, and the planning auto-retry call site documents that it intentionally keeps the normal guard enabled.
- Keep validation focused and avoid broad AWF/GitHub-owned suites: Complete
  - Only targeted tests and focused ruff checks were run locally.

## Evidence

Files changed:

- `src/awf/control/executor/planning_ops.py`
- `src/awf/control/worker/cleanup.py`
- `src/awf/service/workspaces_retry.py`
- `tests/unit/control/test_executor_planning_auto_retry_transactions.py`
- `tests/unit/control/test_worker_parts/test_worker_part_042.py`

Focused failing-before evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_dedupes_repeated_source_runtime_block -q`
  - Failed before implementation because a duplicate blocked event was recorded.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_pending_planning_scope_retry_scan_does_not_rank_plain_manual_retry_events -q`
  - Failed before implementation because the ranked event-type set included `workspace.retry_requested`.

Focused passing evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_dedupes_repeated_source_runtime_block -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_pending_planning_scope_retry_scan_does_not_rank_plain_manual_retry_events -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/control/test_worker_parts/test_worker_part_042.py -q`
  - Passed: 24 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py src/awf/service/workspaces_retry.py tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/control/test_worker_parts/test_worker_part_042.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation, provenance, and merge gating after completion.
