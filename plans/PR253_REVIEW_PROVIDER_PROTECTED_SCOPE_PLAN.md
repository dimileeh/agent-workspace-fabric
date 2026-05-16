# PR253 Review Provider Protected Scope Plan

## Problem Statement And Scope

Address review-level feedback on PR #253. The reviewer raised two behaviors:

- Codex capacity failures on an explicit non-default model fall back to the configured default before same-provider retries.
- Committed protected-scope repair continues into dirty-worktree commit handling after a terminal provider recovery result.

Existing provider recovery tests assert the Codex capacity fallback ordering, so this plan treats that ordering as current policy and does not change it. The implementation scope is the protected-scope committed repair path only.

## Requirements Checklist

- Preserve existing Codex default-model capacity fallback behavior and tests.
- Detect terminal provider recovery outcomes from monitor agent failures distinctly from generic deterministic CLI failures.
- In `_repair_protected_scope_commits_before_push`, short-circuit immediately after a terminal provider recovery outcome so the runner skips unnecessary dirty commit handling.
- Preserve protected-scope push-block termination semantics by returning the original protected-scope block result.
- Add regression coverage proving the dirty commit helper is not called for this terminal provider path.

## Implementation Steps

1. Add a focused unit test for terminal provider recovery during committed protected-scope repair.
2. Extend provider agent error handling to surface a terminal provider outcome without changing retry, fallback, auth, or deterministic behavior.
3. Use that outcome in `_repair_protected_scope_commits_before_push` to log the CLI failure and return the original protected-scope push-block failure immediately.
4. Leave Codex capacity fallback ordering untouched.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Passes, including the new regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default tests/unit/service/test_provider_recovery.py::test_codex_non_default_capacity_falls_back_to_default_model -q`
  - Passes, proving the intentionally preserved capacity fallback policy remains stable.
