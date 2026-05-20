# Bot Thread Blocked Merge Validation

Plan reference: `plans/bot_thread_blocked_merge_PLAN.md`

## Requirement Status

- Preserve newly actionable inline threads routing to `AddressComments`: Complete.
  Existing `tests/unit/runtime/test_pr_monitor.py` coverage passed unchanged.
- Preserve unresolved human-deferred feedback routing to `NotifyHuman`: Complete.
  Existing deferred feedback tests passed unchanged.
- Allow `BLOCKED` / `HAS_HOOKS` to reach `Merge` when only an already-addressed
  bot-authored inline thread remains unresolved: Complete.
  Added `test_protected_state_with_addressed_bot_thread_reaches_merge_attempt`.
- Keep explicit human review blockers unchanged: Complete.
  Existing blocking-review coverage passed unchanged.

## Evidence

- Changed `src/awf/runtime/pr_monitor.py` Step 8 to gate only unresolved
  human-authored inline threads for `BLOCKED` / `HAS_HOOKS`.
- Added regression coverage in `tests/unit/runtime/test_pr_monitor.py`.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor.py -q`:
  `101 passed`.
- Ran `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py tests/unit/runtime/test_pr_monitor.py`:
  passed.

## Gaps

None.
