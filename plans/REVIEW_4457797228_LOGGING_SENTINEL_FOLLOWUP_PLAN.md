# REVIEW 4457797228 Logging and Sentinel Follow-up Plan

## Problem Statement and Scope

Address the two remaining review-level follow-ups from comment
`issue:4457797228`:

- the protected-scope committed-repair "no commit created" warning must not
  report `PROTECTED_SCOPE_PUSH_BLOCKED`, because a clean worktree can still push
  successfully after history was rewritten;
- `_capacity_default_model` should document that `policy_model` is a sentinel
  when no effective default model is available.

Scope is limited to the called-out runtime logging path, the provider recovery
helper comment, focused regression coverage, and this plan/validation evidence.

## Requirements Checklist

- Add or update a regression test proving the clean "no commit created" warning
  does not carry `reason_code=PROTECTED_SCOPE_PUSH_BLOCKED`.
- Remove the misleading protected-scope blocked reason from that warning without
  changing actual push-block failure results.
- Add a concise comment explaining that `policy_model` is a sentinel in the
  implicit default fallback branch.
- Run narrow tests for the touched behavior and relevant lint where practical.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Tighten the existing protected-scope committed-repair log test so it fails
   when the warning contains `PROTECTED_SCOPE_PUSH_BLOCKED`.
2. Run that targeted test to confirm the regression fails before the code
   change.
3. Remove the misleading `reason_code` field from the clean "no commit created"
   warning.
4. Add the sentinel clarification comment in `_capacity_default_model`.
5. Run the targeted regression, provider recovery tests around capacity default
   fallback, and lint for the touched files.
6. Write `plans/REVIEW_4457797228_LOGGING_SENTINEL_FOLLOWUP_VALIDATION.md`.
7. Stage only changed files and commit with a conventional message for comment
   `4457797228`.

## Verification Commands and Pass Criteria

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created -q`
  must fail after the test is tightened.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created -q`
  must pass.
- Capacity fallback guard coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_non_default_capacity_falls_back_to_default_model tests/unit/service/test_provider_recovery.py::test_codex_default_capacity_does_not_fallback_to_itself tests/unit/service/test_provider_recovery.py::test_codex_implicit_default_capacity_does_not_fallback_to_itself tests/unit/service/test_provider_recovery.py::test_codex_capacity_without_effective_default_skips_implicit_fallback -q`
  must pass.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/service/provider_recovery.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  must pass.
