# Review 4620252998 Compose GC Validation

Plan reference: `plans/REVIEW_4620252998_COMPOSE_GC_PLAN.md`

## Requirement Status

- Complete: Retained completed workspaces with merged PRs now receive fallback
  compose teardown while preserved by `WORKSPACE_WITHIN_RETENTION`.
- Complete: Fallback compose teardown remains scoped away from failed/superseded
  retention-preserved workspaces; the predicate requires completed status for
  the shared `WORKSPACE_WITHIN_RETENTION` reason code.
- Complete: If monitor filesystem GC raises after the compose callback has run,
  the tracked compose outcome is still logged and successful teardown permits
  the auth overlay unmount.
- Complete: `_completed_workspace_compose_teardown` distinguishes
  `compose_project is None` from `compose_project == ""`, allowing candidate
  compose metadata to drive teardown when the monitor fallback is blank.
- Complete: Focused tests cover the changed behavior. Full AWF/GitHub
  validation is intentionally left to AWF after agent completion per the
  workspace contract.

## Evidence

Changed files:

- `src/awf/service/gc.py`
- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`
- `plans/REVIEW_4620252998_COMPOSE_GC_PLAN.md`
- `plans/REVIEW_4620252998_COMPOSE_GC_VALIDATION.md`

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_tears_down_compose_for_retained_merged_workspace tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_failed_within_retention_skips_fallback_compose_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_compose_teardown_accepts_empty_monitor_project_with_candidate_metadata tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown -q
```

Result: `4 passed in 3.55s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/service/test_gc_parts/test_gc_part_001.py tests/unit/runtime/test_monitor_completion_gc.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py
```

Result: `Success: no issues found in 2 source files`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_tears_down_compose_for_preserved_workspace tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_cleanup_disabled_skips_fallback_compose_teardown tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_triage_preserved_skips_fallback_compose_teardown tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_fallback_compose_teardown_releases_runtime_side_effects tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_compose_teardown_callback_uses_candidate_metadata tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tears_down_compose_when_plan_is_empty tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_unmounts_auth_overlay_when_plan_is_empty -q
```

Result: `7 passed in 6.75s`
