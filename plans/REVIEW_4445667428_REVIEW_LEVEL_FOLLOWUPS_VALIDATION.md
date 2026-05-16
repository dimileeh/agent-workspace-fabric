# Review 4445667428 Review-Level Followups Validation

Plan reference:
`plans/REVIEW_4445667428_REVIEW_LEVEL_FOLLOWUPS_PLAN.md`

## Requirement Status

- Complete: Added regression coverage that the event-order migration carries
  PostgreSQL timeout guardrails, then added `lock_timeout` and
  `statement_timeout` guards to the migration.
- Complete: Added repository regression coverage proving `add_events()`
  advances `workspace.version` and assigns event orders after the existing
  latest order while preserving `workspace.updated_at` for event-only writes.
- Complete: Centralized event-order reservation in
  `WorkspaceRepository` through an atomic workspace-row version update. The
  reservation preserves `updated_at` so append-only event and audit rows do not
  reset retention or ordering cutoffs.
- Complete: Adjusted stale manual version expectations and retained the
  remonitor state-reset manual event path where custom old/new states require a
  direct event append.
- Complete: Added callback delivery coverage proving
  `workspace.secondary_failure_recorded` uses the sanitized public workspace
  envelope and does not expose internal causality payload keys.
- Complete: Updated callback API docs with valid public event examples and the
  stable sanitized secondary-failure callback envelope shape.
- Complete: Focused and full validation passed.
- Complete: Local commit is prepared after this validation file.
- Complete: The required `AWF-VERDICT` line will be emitted after the local
  commit.

## Evidence

Changed files:

- `docs/REST_API_REFERENCE.md`
- `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
- `src/awf/db/repositories.py`
- `src/awf/service/controls.py`
- `tests/unit/api/test_workspace_controls_idempotency.py`
- `tests/unit/db/test_migration_graph.py`
- `tests/unit/db/test_workspace_repository.py`
- `tests/unit/service/test_callbacks.py`
- `tests/unit/service/test_controls.py`
- `tests/unit/service/test_controls_lifecycle.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/REVIEW_4445667428_REVIEW_LEVEL_FOLLOWUPS_PLAN.md`
- `plans/REVIEW_4445667428_REVIEW_LEVEL_FOLLOWUPS_VALIDATION.md`

TDD failure confirmed before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_reserves_event_order_and_advances_workspace_version -q
```

Result: failed as expected because the migration had no timeout guardrails and
`add_events()` reused the current `workspace.version` without advancing it.

Focused verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_reserves_event_order_and_advances_workspace_version -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_secondary_failure_callback_envelope_excludes_internal_causality_payload tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure tests/unit/service/test_failure_causality.py::test_remonitor_reset_event_order_precedes_same_tick_failure tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_merge_queue.py::TestMergeQueueList::test_filters_by_repo_base_status_and_limit tests/unit/control/test_worker.py::TestTerminalRuntimeRelease::test_release_retry_marker_does_not_advance_updated_at tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_filesystem_gc_revokes_active_secret_leases tests/unit/service/test_gc.py::test_single_workspace_gc_revokes_active_secret_leases_before_auth_cleanup tests/unit/service/test_gc.py::test_batch_terminal_gc_revokes_each_candidate_and_is_retry_safe -q
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py tests/unit/db/test_migration_graph.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py tests/unit/service/test_controls_lifecycle.py tests/unit/api/test_workspace_controls_idempotency.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_reserves_event_order_and_advances_workspace_version -q
```

Results: all focused verification commands passed.

Broad verification:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

Results:

- Ruff passed.
- Mypy passed: no issues in 154 source files.
- Full unit suite passed: 6030 passed, 1 deprecation warning from
  `openapi_spec_validator`.

## Gaps

None.
