# Review Issue 4620252998 Compose Teardown Context Validation

Plan reference: `plans/review_issue_4620252998_compose_teardown_context_PLAN.md`

## Requirement Status

- Add a focused regression test proving a known monitor compose project still
  builds and runs a teardown callback when `compose_file=None`: Complete.
  Added
  `test_completed_workspace_compose_teardown_uses_project_when_compose_file_missing`.
- Keep label-fallback behavior compatible with `ComposeManager.teardown_project`
  by passing a deterministic missing compose-file path when no persisted or
  monitor compose file is available: Complete. `_compose_file_for_gc_candidate`
  now returns the candidate compose path plus `compose.yml` when no persisted or
  monitor path exists.
- Document that the preserved-workspace fallback is for non-monitor or future
  callers because production monitor GC bypasses retention with
  `ignore_retention=True`: Complete. Added a focused comment at the fallback
  selection branch in `run_workspace_filesystem_gc`.
- Preserve existing behavior for callers with no compose project context:
  Complete. `_completed_workspace_compose_teardown` still returns `None` when
  `compose_project is None`.
- Do not change protected workflow, quality-gate, or broad validation files:
  Complete. Only source, targeted unit test, and plan/validation docs changed.
- Run only focused tests or checks for the changed files; full AWF/GitHub
  validation remains post-agent owned: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `src/awf/service/gc.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`
- `plans/review_issue_4620252998_compose_teardown_context_PLAN.md`
- `plans/review_issue_4620252998_compose_teardown_context_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_compose_teardown_uses_project_when_compose_file_missing -q`
  failed before the implementation with `assert None is not None`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_compose_teardown_uses_project_when_compose_file_missing -q`
  passed after the implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q`
  passed: 26 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/service/gc.py tests/unit/runtime/test_monitor_completion_gc.py`
  passed.

The callback-exception tracker concern from issue 3 remains unchanged because
the current implementation already re-raises into `_run_gc_compose_teardowns`,
and the focused monitor cleanup test file includes existing coverage for the
tracked callback-raised path. Full AWF/GitHub validation is intentionally left
to AWF after agent completion.
