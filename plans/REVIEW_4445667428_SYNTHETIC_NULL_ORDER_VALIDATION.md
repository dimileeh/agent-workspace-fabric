# Review 4445667428 Synthetic Null Order Validation

Plan reference: `plans/REVIEW_4445667428_SYNTHETIC_NULL_ORDER_PLAN.md`

## Requirement Status

- Keep the AWF current-branch workflow intact; do not switch branches or push:
  Complete. No branch or remote operations were used.
- Add or update regression tests before production changes: Complete. Updated
  the already-failed cleanup test to require a synthetic marker and added an
  unordered same-tick failure epoch regression before changing production code.
- Mark the already-failed cleanup synthetic state-change payload with an
  explicit machine-readable key: Complete. The synthetic cleanup event payload
  now carries `"synthetic": true`.
- Ensure same-timestamp epoch reset rows do not invalidate an unordered
  reference failure event: Complete. Unordered reference comparisons now include
  only strictly later/earlier rows plus the reference row itself.
- Preserve ordered same-tick epoch reset behavior: Complete. Existing
  same-tick reset coverage now uses explicit `event_order` values and still
  expects the later ordered reset to be detected.
- Run focused validation for the touched controls and failure-causality code:
  Complete.
- Commit local changes with a conventional commit referencing review comment
  `4445667428`: Complete. This validation file is included in the local fix
  commit.
- Emit the required `AWF-VERDICT` line when complete: Complete. The verdict is
  emitted after the local commit as required by the AWF comment-handling
  contract.

## Evidence

TDD failure confirmed before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_failure_causality.py::test_epoch_reset_detection_ignores_same_tick_reset_when_reference_event_is_unordered -q
```

Result: failed with missing `payload["synthetic"]` and missing preserved
failure-event `reason_code` after the same-timestamp reset suppressed the
unordered failed event.

Final verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed tests/unit/service/test_failure_causality.py::test_epoch_reset_detection_ignores_same_tick_reset_when_reference_event_is_unordered -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/failure_causality.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py
```

Results: all final verification commands passed.

## Gaps

No implementation gaps remain.
