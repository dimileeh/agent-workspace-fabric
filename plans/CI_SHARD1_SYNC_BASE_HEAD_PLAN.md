# CI shard 1 sync-base head plan

## Problem statement and scope

PR #614 also fails GitHub Actions `python-coverage-shards (1)` in sync-base
monitor integration tests. The failures show sync-base returning
`REPAIR_START_HEAD_UNAVAILABLE` before `merge --abort`/merge/push, because the
new operation-start HEAD probe consumes the first queued git command in tests
and blocks the path before the behavior under test.

Scope is limited to the sync-base operation-start baseline. Do not edit
workflow/config files or run broad validation.

## Requirements checklist

- [ ] Preserve AWF branch ownership: no branch switch, push, rebase, or broad
  AWF/GitHub-owned validation.
- [ ] Reproduce shard-1 failures from CI logs with focused pytest targets.
- [ ] Keep the sync-base operation-start baseline stable without adding an
  unnecessary git probe when PR status already provides the head SHA.
- [ ] Keep direct `_run_sync_base` tests without a supplied PR head exercising
  the explicit rev-parse fallback.
- [ ] Run focused shard-1 pytest targets and touched-file Ruff.
- [ ] Record validation evidence in `plans/CI_SHARD1_SYNC_BASE_HEAD_VALIDATION.md`.
- [ ] Commit the scoped fixes locally with a conventional commit message.

## Implementation steps

1. Use the supplied `pr_head_sha` as sync-base `operation_start_head` when present.
2. Leave `_repair_operation_start_head_result` in place for direct callers that
   do not provide `pr_head_sha`.
3. Remove any now-unneeded test queue entries that modeled the extra probe.
4. Re-run focused sync-base shard-1 tests plus the shard-6/shard-8 focused checks.

## Verification commands and pass criteria

- Focused shard-1 pytest targets pass.
- Focused shard-6 pytest targets remain passing.
- The line-limit guardrail remains passing.
- Ruff passes for touched source and test files.
- Full AWF/GitHub validation and coverage gates remain managed by AWF after
  agent completion.
