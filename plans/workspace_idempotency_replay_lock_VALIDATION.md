# Workspace Idempotency Replay Lock Validation

Plan reference: `plans/workspace_idempotency_replay_lock_PLAN.md`

## Requirement Status

- Complete: Added a regression proving over-quota duplicate workspace creates
  replay after the idempotency lock even when `has_idempotency_key()` would miss.
  Evidence: `tests/unit/api/test_workspaces.py`.
- Complete: Preserved rate limiting for genuinely fresh over-quota idempotency
  keys. Evidence: updated fresh-key rate-limit assertions continue to expect
  `429`.
- Complete: Preserved the no full-table replay-key warmup behavior for rejected
  fresh keys. Evidence: test still fails if `list_idempotency_replay_keys()` is
  called.
- Complete: Applied the same ordering semantics to v1 and v2 create helpers.
  Evidence: `src/awf/api/routes/workspaces.py` updates both durable replay
  helpers.
- Complete: Kept changes scoped to the route helper and focused API tests.

## Verification Evidence

- Failing-before check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -k "lock_scoped_replay_check or waits_on_replay_lock_when_probe_misses" -q`
  failed with four failures before implementation.
- Passing focused check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -k "lock_scoped_replay_check or waits_on_replay_lock_when_probe_misses" -q`
  passed: `4 passed, 135 deselected`.
- Passing file check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passed: `139 passed`.
- Passing lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  passed.
- Passing type check:
  `uv run --python 3.12 --extra dev mypy src/awf/api/routes/workspaces.py`
  passed.

## Gaps

None.
