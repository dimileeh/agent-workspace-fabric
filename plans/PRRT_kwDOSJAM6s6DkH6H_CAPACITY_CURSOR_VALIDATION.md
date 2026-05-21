# Capacity Cursor Age Refresh Validation

Plan reference: `PRRT_kwDOSJAM6s6DkH6H_CAPACITY_CURSOR_PLAN.md`

## Requirement Status

- Add a regression test for age-threshold cursor invalidation: Complete.
  Evidence: `tests/unit/control/test_worker.py` adds
  `test_requested_capacity_gate_resets_resume_cursor_when_age_boost_threshold_changes`.
  Before the implementation, the test failed with `assert 0 == 1` on the second
  worker poll.
- Keep bounded capacity resume behavior when scheduler age buckets are
  unchanged: Complete.
  Evidence: `test_requested_capacity_gate_resumes_after_bounded_blocked_scan`
  still passes in the requested-capacity selection.
- Reset the inter-poll requested-capacity cursor when requested candidates cross
  an age-boost threshold: Complete.
  Evidence: `src/awf/control/worker.py` gates resume cursor reuse through
  `_requested_capacity_age_boost_changed`.
- Preserve existing queue/allocation/provider-suppression invalidation behavior:
  Complete.
  Evidence: the requested-capacity gate test selection passes, including existing
  queue-change and provider-suppression cursor reset cases.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "age_boost_threshold"` failed before the fix with `assert 0 == 1`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "age_boost_threshold or requested_capacity_gate_resumes_after_bounded_blocked_scan or requested_capacity_gate_resets_resume_cursor_when_requested_queue_changes"` passed: `3 passed, 217 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate"` passed: `22 passed, 198 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py` passed.

## Gaps

No known gaps.
