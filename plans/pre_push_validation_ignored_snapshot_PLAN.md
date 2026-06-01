# Plan: Guard pre-push validation retries against newly ignored snapshots

## Problem statement and scope
The pre-push validation retry loop can run a full validation cycle before detecting that ignored worktree snapshot data grew relative to the initial baseline, even though the result should fail fast with `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.

Scope is limited to `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and existing pre-push retry unit coverage in `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`.

## Requirements
- [ ] Detect ignored-root/snapshot/snapshot-signature gains before running validation commands on retry passes.
- [ ] Preserve existing behavior for first attempt baseline capture and success/failure handling.
- [ ] Keep the user-facing failure reason/message for gained ignored entries.
- [ ] Add or update a unit test that demonstrates guard-before-validation for gained ignored paths.

## Implementation steps
1. Extend `_run_pre_push_validation` to accept optional baseline ignored roots/snapshot/snapshot-signature inputs.
2. During pre-validation check in `_run_pre_push_validation`, compare current ignored state to the baseline and return a `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` result when gains are detected.
3. Pass baseline baseline state from `_run_pre_push_validation_with_fix_passes` into subsequent retry calls.
4. Add a focused unit test that patches the pre-push worktree check to return a baseline gain and asserts validation command execution is skipped.

## Verification commands
- Run targeted pytest tests for the new/updated pre-push validation regression cases only.
