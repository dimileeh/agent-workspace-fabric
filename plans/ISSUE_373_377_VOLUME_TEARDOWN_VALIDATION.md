# Issue #373/#377 Volume Teardown Validation

Plan reference: `plans/ISSUE_373_377_VOLUME_TEARDOWN_PLAN.md`

## Requirement Status

- Complete: Missing compose files in `ComposeManager.teardown_project` fall back to label-scoped teardown.
- Complete: The genuine no-resource missing-compose case stays idempotent and successful.
- Complete: Label fallback failures preserve specific `ComposeOperationError.reason_code` values.
- Complete: Post-merge monitor filesystem GC passes a volume-removing `compose_teardown` callback into `run_workspace_filesystem_gc`.
- Complete: Post-merge compose teardown failures gate filesystem deletion through the GC engine result.
- Complete: Tests were written/updated before lifecycle implementation changes; compose-manager behavior was already implemented and covered in the checkout.
- Complete: Validation used focused checks; full AWF/GitHub validation and the full repository coverage gate remain post-agent responsibilities.

## Files Changed

- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`
- `tests/unit/runtime/test_pr_monitor_manual_merge.py`
- `plans/ISSUE_373_377_VOLUME_TEARDOWN_PLAN.md`
- `plans/ISSUE_373_377_VOLUME_TEARDOWN_VALIDATION.md`

`src/awf/node/compose_manager.py` already satisfied the #373 contract in this checkout, including the missing-file label fallback and focused regression tests, so no source edit was needed there.

## Evidence

Focused #373/#377 behavior:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_reaps_stale_volumes_when_compose_file_missing tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_is_idempotent_when_nothing_left_to_reap tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_fails_loud_when_label_probe_unavailable tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_passes_volume_reaping_compose_teardown_to_filesystem_gc tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails -q
```

Result: `5 passed in 3.37s`

Focused final behavior set including old raw-teardown manual-merge assumptions:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_reaps_stale_volumes_when_compose_file_missing tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_is_idempotent_when_nothing_left_to_reap tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_fails_loud_when_label_probe_unavailable tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_passes_volume_reaping_compose_teardown_to_filesystem_gc tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails tests/unit/runtime/test_pr_monitor_manual_merge.py::test_manual_merge_external_merge_completes_with_monitor_done_and_cleanup tests/unit/runtime/test_pr_monitor_manual_merge.py::test_release_monitor_factory_uses_manual_merge_contract -q
```

Result: `7 passed in 7.58s`

Additional affected terminal monitor cases:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_notify_human_action_emits_log_line tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_notify_human_keeps_polling_and_addresses_later_comments tests/unit/runtime/test_defer_signal_artifact.py::TestDeferSignalArtifact::test_merge_blocked_notification_waits_until_external_merge_for_artifact tests/unit/runtime/test_defer_signal_artifact.py::TestDeferSignalArtifact::test_human_defer_notification_waits_until_external_merge_for_artifact -q
```

Result: `4 passed in 9.77s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_merge_action_emits_log_line tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_bot_issue_feedback_stays_alive_and_addresses_later_comments tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_review_disabled_comment_routes_to_agent_before_merge tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_pre_merge_recheck_can_block_merge_on_late_review_comment tests/unit/runtime/test_merge_coordinator_runner.py::TestMergeCoordinatorRunner::test_manual_auto_merge_false_monitoring_never_enters_coordinator -q
```

Result: `5 passed in 11.10s`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_actionable_bot_comments.py::test_bot_review_boilerplate_routes_to_agent_without_human_wait tests/unit/runtime/test_pr_monitor_non_actionable_bot_comments.py::test_bot_issue_boilerplate_defer_does_not_notify_human -q
```

Result: `2 passed in 2.59s`

Static checks:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/compose_manager.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/node/test_compose_manager.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_pr_monitor_manual_merge.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf/node/compose_manager.py src/awf/runtime/pr_monitor_runner/lifecycle.py
```

Result: `Success: no issues found in 2 source files`

Coverage evidence:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_reaps_stale_volumes_when_compose_file_missing tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_is_idempotent_when_nothing_left_to_reap tests/unit/node/test_compose_manager.py::TestRender::test_teardown_project_fails_loud_when_label_probe_unavailable --cov=awf.node.compose_manager --cov-report=term-missing --no-cov-on-fail --cov-fail-under=0 -q
```

Result: `3 passed in 0.52s`; scoped report showed `awf.node.compose_manager` at `37.35%` for this intentionally narrow test selection.

The combined focused coverage command for both changed modules was attempted twice, plus once with `COVERAGE_CORE=sysmon`; each run crashed before executing tests with a Python segmentation fault in asyncpg during pytest collection-time stale Postgres schema cleanup (`tests/conftest.py::pytest_collection_finish` -> `tests/postgres.py::_list_stale_postgres_test_schemas`). This is an environment/tooling crash in the local coverage run, not a test assertion failure. Full coverage validation remains owned by AWF/GitHub after agent completion.

## Remaining Gaps

No planned implementation gaps remain.
