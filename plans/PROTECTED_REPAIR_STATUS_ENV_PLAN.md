# Protected Repair Status Env Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K6yrE` reports that `_repair_protected_scope_changes_before_commit` reads post-agent `git status --porcelain` without stripping inherited Git object lookup environment variables. The scope is limited to that post-repair status command and a focused regression assertion.

## Requirements

- Use `git_env_without_object_lookup_overrides()` for the post-repair `git status --porcelain` command.
- Preserve existing protected-scope repair behavior and result handling.
- Add focused regression coverage proving object lookup env vars are stripped from that status command.
- Run only targeted validation for the changed behavior; broad AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Import `git_env_without_object_lookup_overrides` in `remote_repair_protected.py`.
2. Pass the sanitized env to the repaired status command in `_repair_protected_scope_changes_before_commit`.
3. Extend the focused protected repair unit test to set poisoned Git object lookup env vars and assert the status command strips them.

## Verification

- Targeted pytest for the modified unit test should pass.
- A narrow syntax/import check may be used if needed.
