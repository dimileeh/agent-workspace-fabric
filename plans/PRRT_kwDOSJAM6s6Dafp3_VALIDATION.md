# PRRT_kwDOSJAM6s6Dafp3 Validation

Plan reference: `PRRT_kwDOSJAM6s6Dafp3_PLAN.md`

## Requirement Status

- Complete: Added a PostgreSQL-backed regression test,
  `test_salvage_not_possible_recording_serializes_concurrent_events`, that fails
  when the second concurrent writer reaches the salvage-event guard before the
  first writer commits.
- Complete: `_record_preserved_active_salvage_not_possible` now loads the
  workspace via `WorkspaceRepository.get_for_update` before
  `_has_current_salvage_event` and `add_event`.
- Complete: Existing status checks, event-floor filtering, payload shape, and
  no-op behavior are preserved; the only production code change is the locked
  workspace fetch.
- Complete: No unrelated salvage flows were changed.
- Complete: Targeted and focused validation commands passed.

## Evidence

- Changed `src/awf/control/worker.py` to acquire the workspace row lock in the
  not-possible salvage recording transaction.
- Changed `tests/unit/control/test_worker.py` to cover concurrent not-possible
  salvage writers for the same workspace and preserved epoch.
- Confirmed the regression failed before the implementation change:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k test_salvage_not_possible_recording_serializes_concurrent_events -q`
  failed with `Failed: DID NOT RAISE <class 'TimeoutError'>`.
- After the implementation change, the targeted regression passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k test_salvage_not_possible_recording_serializes_concurrent_events -q`
  returned `1 passed, 199 deselected`.
- Lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`.
- Focused worker module passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  returned `200 passed in 215.69s`.

## Remaining Gaps

None.
