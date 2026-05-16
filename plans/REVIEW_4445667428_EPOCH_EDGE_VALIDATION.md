# Review 4445667428 Epoch Edge Validation

Plan reference: `plans/REVIEW_4445667428_EPOCH_EDGE_PLAN.md`

## Requirement Status

- Keep the AWF current-branch workflow intact; do not switch branches or push:
  Complete. No branch or remote operations were used.
- Add regression coverage before the production change: Complete. Added tests
  for remonitor `state_reset.to` reset detection and provisioning epoch reset
  isolation; both failed before the implementation change.
- Treat `provisioning` as a failure epoch reset state: Complete.
  `_FAILURE_EPOCH_RESET_STATES` now includes
  `WorkspaceStatus.provisioning.value`.
- Detect `workspace.remonitor_requested` epoch resets from
  `payload.state_reset.to` instead of depending on the event row `new_state`:
  Complete. The reset predicate now applies `new_state` only to
  `workspace.state_changed` rows and uses JSON `state_reset.to` for remonitor
  reset rows.
- Preserve existing state-change reset behavior and primary failure causality
  payload semantics: Complete. The full focused failure causality unit file
  passes.
- Run narrow validation for the touched failure causality behavior: Complete.
- Commit local changes with a conventional commit referencing review comment
  `4445667428`: Complete. This validation file is part of that local commit.

## Evidence

- Changed `src/awf/service/failure_causality.py`.
- Changed `tests/unit/service/test_failure_causality.py`.
- Added this plan/validation pair under `plans/`.

## Verification

- Initial regression run before the implementation change failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_epoch_reset_detection_reads_remonitor_state_reset_target tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_uses_current_failure_after_provisioning_reset -q`
  failed with both new tests red.
- After the implementation change, the same targeted command passed with
  2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passed with 19 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py`
  passed.

No remaining planned gaps.
