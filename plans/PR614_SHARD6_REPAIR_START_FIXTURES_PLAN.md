# PR614 Shard 6 Repair Start Fixtures Plan

## Problem Statement and Scope

PR #614 also fails GitHub Actions `python-coverage-shards (6)` in run
`27858562982`. The failing tests cluster around CI-fix provider recovery and
commit-sink behavior. The observed failures show repair-start baseline capture
returning `REPAIR_START_HEAD_UNAVAILABLE` before the tests reach the behavior
they intend to exercise. One missing-HEAD no-mirror test also only queues one
failed object lookup even though the implementation now verifies both the stale
operation start and the candidate recovery head.

Scope is limited to focused test-fixture corrections so these tests provide the
repair-start baseline required by the current monitor flow and continue to
assert their original behavior.

## Requirements Checklist

- [ ] Do not switch branches, push, rebase, or run broad AWF/GitHub-owned
      validation.
- [ ] Reproduce representative shard 6 failures locally before editing.
- [ ] Preserve the original behavior assertions for provider recovery,
      commit-sink error precedence, and unverified no-mirror candidate rejection.
- [ ] Add only the minimum worktree setup and queued git results needed for
      repair-start baseline capture in the affected tests.
- [ ] Run focused local tests covering the affected failures.
- [ ] Record focused evidence in a validation document and note that full
      AWF/GitHub validation remains owned by AWF after agent completion.
- [ ] Commit the scoped fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Run a representative failing CI-fix provider/commit-sink test locally.
2. Run the no-mirror unverified candidate test locally.
3. Add a small local test helper, if useful, to create the repair worktree and
   queue clean status, `rev-parse HEAD`, and `cat-file -e` success results.
4. Apply that setup to the affected CI-fix tests in shard 6.
5. Update the no-mirror candidate test to queue failures for both checked
   anchors and assert both object checks.
6. Run focused tests for the affected shard 6 failures.
7. Create `plans/PR614_SHARD6_REPAIR_START_FIXTURES_VALIDATION.md` with command
   evidence and residual risk.

## Verification Commands and Pass Criteria

- Representative failing tests must fail before the fixture update.
- Focused affected tests must pass after the fixture update.
- Do not run full coverage, all unit tests, frontend builds, or CI-equivalent
  validation locally.
