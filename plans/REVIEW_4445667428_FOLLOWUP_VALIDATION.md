# Review 4445667428 Followup Validation

Plan reference: `plans/REVIEW_4445667428_FOLLOWUP_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for a current-epoch stream where the
  primary event is older than the latest failed event and the latest event has
  `secondary_failures` without embedded `primary_failure`.
- Complete: Added regression coverage proving
  `restore_primary_failure_row_fields` clears `workspace.failure_message` when
  the primary snapshot has no message.
- Complete: `load_failure_causality_snapshot` now keeps primary evidence
  anchored to the selected current-epoch primary event while merging secondary
  histories from failed events through the latest failed event in that epoch.
- Complete: Existing singleton and accumulated secondary payload shapes remain
  supported; overlapping histories are merged without duplicating the shared
  prefix.
- Complete: Cleanup's already-failed branch now documents why it increments the
  workspace version before writing the synthetic state-change event.
- Complete: Focused tests, lint, and type checks passed.

## Evidence

Changed files:

- `src/awf/service/failure_causality.py`
- `src/awf/service/controls.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/REVIEW_4445667428_FOLLOWUP_PLAN.md`
- `plans/REVIEW_4445667428_FOLLOWUP_VALIDATION.md`

TDD failure confirmed before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_merges_secondary_history_from_latest_failed_event_without_embedded_primary tests/unit/service/test_failure_causality.py::test_restore_primary_failure_row_fields_clears_missing_failure_message -q
```

Result: failed with the latest secondary missing from
`snapshot.secondary_failures` and stale `workspace.failure_message` still set.

Final verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py
uv run --python 3.12 --extra dev ruff format --check src/awf/service/failure_causality.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py
uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py src/awf/service/controls.py
git diff --check
```

Results: all final verification commands passed.

## Gaps

None.
