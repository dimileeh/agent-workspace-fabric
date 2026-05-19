# Plan: Fix Monitor Circuit Suppression Dedup for No-Cooldown Breaker State

## Problem statement and scope
Fix review comment #4484912614 for `src/awf/runtime/pr_monitor_runner.py`: `provider_cooldown_not_before(task_policy) != breaker.cooldown_until` causes a deduplicated monitor suppression path to consider an absent/no-cooldown breaker (`breaker.cooldown_until is None`) as changed every call, rewriting workspace task policy and incrementing versions repeatedly.

## Requirements
- [ ] Ensure `_provider_recovery_suppresses_cli` does not rewrite workspace task policy on every poll when provider cooldown is open with `cooldown_until=None` and recovery state already matches.
- [ ] Preserve the existing one-time event emission behavior and durable state for first-time open-breaker detection.
- [ ] Preserve existing behavior for non-`None` cooldown updates (including stale task-policy refresh tests).
- [ ] Keep changes scoped to the reported dedup-path issue in `pr_monitor_runner.py` only.

## Implementation steps
1. Update the dedup condition in `_provider_recovery_suppresses_cli` around the recovery-state refresh block.
2. Compute and use a stable comparison that does not treat `cooldown_until=None` as unequal to persisted `not_before` when state is already correct.
3. If needed, adjust the no-cooldown branch so persisted state is durable but does not trigger future rewrite on each poll.

## Validation plan
- Run focused tests in `tests/unit/runtime/test_pr_monitor_runner.py` around no-cooldown dedup and stale refresh paths.
- Re-run full `pytest tests/unit/runtime -q` if lightweight targeted tests are green.
