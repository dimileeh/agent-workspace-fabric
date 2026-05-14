# Review 4445667428 Followup Plan

## Problem Statement And Scope

Address the current review-level comment `issue:4445667428` for failure
causality preservation. Scope is limited to three follow-up issues:

- Preserve secondary failure history when the latest failed event in an epoch
  does not embed `primary_failure` but an earlier current-epoch event does.
- Restore `workspace.failure_message` consistently from primary failure
  evidence, clearing stale secondary messages when the primary has no message.
- Document why the already-failed cleanup path manually increments
  `workspace.version` before adding a synthetic `workspace.state_changed`
  event.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Add a regression test that fails under the current two-stage event query when
  secondary history exists on a newer failed event without embedded primary
  evidence.
- Add or update regression coverage for clearing `workspace.failure_message`
  when the preserved primary failure has no message.
- Implement the smallest failure-causality change that combines current-epoch
  secondary history without leaking across epoch resets.
- Keep `secondary_failure`/`secondary_failures` behavior compatible with
  existing preserved payloads and legacy singleton payloads.
- Add the cleanup-path explanatory comment without changing cleanup behavior.
- Run focused tests and lint/type checks for touched modules.
- Commit the local fix with a conventional commit message referencing the
  review comment id.
- Emit the required `AWF-VERDICT` line when complete.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_failure_causality.py` for the
   mixed primary/secondary event stream and message-clearing behavior.
2. Confirm those tests fail before implementation.
3. Update `src/awf/service/failure_causality.py` so primary evidence can come
   from the earlier current-epoch primary event while secondary history is
   merged from current-epoch failed events through the latest failed event.
4. Make `restore_primary_failure_row_fields` assign `failure_message`
   unconditionally, bounded when present and `None` when absent.
5. Add the explanatory comment in `src/awf/service/controls.py`.
6. Re-run focused tests plus ruff/mypy on the touched surface.
7. Create `plans/REVIEW_4445667428_FOLLOWUP_VALIDATION.md` with requirement
   status and command evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_merges_secondary_history_from_latest_failed_event_without_embedded_primary tests/unit/service/test_failure_causality.py::test_restore_primary_failure_row_fields_clears_missing_failure_message -q`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py src/awf/service/controls.py`
  must pass.
