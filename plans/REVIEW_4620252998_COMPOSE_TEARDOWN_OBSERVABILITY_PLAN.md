# Review 4620252998 Compose Teardown Observability Plan

## Problem statement and scope

Greptile's review-level comment reports that the post-merge completion path now
delegates Docker compose teardown through filesystem GC and no longer emits the
historical `monitor.compose_teardown_ok` structured log event. It also notes
that the legacy `_teardown_compose_stack` helper is no longer part of the main
completion flow and could mislead future callers.

Scope is limited to PR monitor completion GC observability and a compatibility
comment on the legacy helper. The already-present teardown-only template
sentinel and corrected teardown-failure assertion are treated as verified stale
sub-issues unless current code contradicts that.

## Requirements checklist

- Restore a direct structured compose teardown log event from the GC-backed
  completion path when GC records a compose teardown result for the workspace.
- Preserve the aggregate filesystem GC events and failure gate semantics.
- Do not reintroduce raw `docker compose down` into `_terminate_completed`.
- Mark `_teardown_compose_stack` as a legacy compatibility helper so future
  contributors do not mistake it for the completion GC path.
- Keep tests focused on the changed completion-GC behavior.

## Implementation steps

1. Update focused completion-GC tests to expect direct compose teardown success
   and failure log events from the GC-backed path.
2. Confirm the updated focused tests fail before the implementation change when
   practical.
3. Add a small lifecycle helper that logs `monitor.compose_teardown_ok` or
   `monitor.compose_teardown_failed` from `WorkspaceGCResult.compose_teardowns`
   for the completed workspace.
4. Call that helper after `run_workspace_filesystem_gc` returns and before the
   aggregate filesystem GC event handling.
5. Add a docstring note to `_teardown_compose_stack` that it is retained only as
   a legacy compatibility surface.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_reclaims_recent_workspace_pressure_dirs_immediately tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails -q`
  - Passes after implementation.
  - Fails before implementation because the direct compose teardown events are
    absent.

Full AWF/GitHub validation is intentionally left to AWF after agent completion
per the workspace contract.
