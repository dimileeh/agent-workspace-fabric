# Review 4445667428 Event Order Null Plan

## Problem Statement And Scope

PR review comment `issue:4445667428` flags that same-timestamp failure epoch
reset detection can treat a reset event with `event_order = NULL` as occurring
at or after an ordered failure event. Because `WorkspaceRepository.add_event`
does not currently stamp `event_order`, future `workspace.remonitor_requested`
reset events can also land in that ambiguous bucket and suppress valid primary
failure evidence.

Scope is limited to event ordering metadata for repository-added events,
failure causality same-timestamp predicates, and focused regression coverage.

## Requirements Checklist

- Keep the AWF current-branch workflow intact; do not switch branches or push.
- Add failing regression coverage before production changes.
- Stamp repository-added events with the current workspace-local event order so
  reset events created through `add_event` can be ordered against state changes.
- Prevent ordered failure events from being suppressed by same-timestamp legacy
  reset rows whose `event_order` is `NULL`.
- Preserve existing failure epoch reset behavior when both events have
  `event_order` values or when the failure event itself is legacy unordered.
- Run focused validation for failure causality and touched repository behavior.
- Commit local changes with a conventional commit referencing review comment
  `4445667428`.

## Implementation Steps

1. Add regression tests covering an ordered failure with a same-timestamp NULL
   reset row and a repository-created remonitor reset that must carry
   `event_order`.
2. Run the new focused tests before implementation and confirm they fail.
3. Populate `WorkspaceEvent.event_order` in `WorkspaceRepository.add_events`
   from the current `workspace.version`.
4. Tighten same-timestamp causality predicates so ordered events only compare
   against reset rows with non-NULL `event_order`.
5. Re-run the focused tests plus failure causality coverage and lint/type
   checks as practical.
6. Record validation evidence in
   `plans/REVIEW_4445667428_EVENT_ORDER_NULL_VALIDATION.md`.

## Verification Commands And Pass Criteria

- Targeted new regression tests fail before implementation and pass after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf` passes.
