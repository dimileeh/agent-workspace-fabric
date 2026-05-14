# PRRT_kwDOSJAM6s6CEHoI Plan

## Problem Statement and Scope

The remonitor path that resets a failed workspace back to `monitoring_pr` appends a
`workspace.remonitor_requested` event after manually incrementing
`workspace.version`. Other event-writing paths reserve event orders through
`WorkspaceRepository._reserve_workspace_event_orders`, which performs an atomic
database update. This plan addresses only that state-reset remonitor event path.

## Requirements Checklist

- Add a regression test proving the failed-workspace remonitor reset path reserves
  exactly one event order through the shared reservation helper.
- Replace the Python-side `workspace.version += 1` assignment with the shared
  atomic reservation before appending the explicit old/new state event.
- Preserve existing remonitor response payloads, operation results, event payloads,
  and old/new state semantics.
- Keep the change local to the review thread and avoid unrelated refactors.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_controls_lifecycle.py` that
   monkeypatches `WorkspaceRepository._reserve_workspace_event_orders` and verifies
   the failed-workspace remonitor reset path calls it with `count=1`.
2. Run the new test before implementation and confirm it fails because the current
   path uses a manual version increment.
3. Update `src/awf/service/controls.py` so the state-reset branch reserves an event
   order through the repository helper and assigns that value to the event.
4. Re-run the focused remonitor lifecycle tests and lint/type checks as appropriate
   for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes or any pre-existing unrelated failure is documented in validation.
