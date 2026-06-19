# CI shard 6 PR-monitor tests plan

## Problem statement and scope

PR #614 also fails GitHub Actions `python-coverage-shards (6)` with focused
PR-monitor unit tests. The failures reproduce locally and are concentrated in
pre-push recovered-HEAD validation, protected-scope pause state, sync-base
protected-scope diff helpers, and fix-cycle recovery-anchor tests.

Scope is limited to fixing real behavior or updating narrow test fakes that no
longer model the current guarded git command sequence. Do not edit protected
workflow/config files, do not weaken checks, and do not run broad AWF-owned
validation.

## Requirements checklist

- [ ] Preserve AWF branch ownership: no branch switch, push, rebase, or broad
  AWF/GitHub-owned validation.
- [ ] Reproduce the shard-6 failures with focused pytest targets before editing.
- [ ] Keep production changes minimal; prefer test harness fixes when production
  behavior is already correct.
- [ ] Preserve reason-code assertions for protected-scope and ownership failures.
- [ ] Run focused pytest for the nine failing shard-6 tests after the fix.
- [ ] Run focused Ruff over touched files.
- [ ] Record validation evidence in `plans/CI_SHARD6_PR_MONITOR_TESTS_VALIDATION.md`.
- [ ] Commit the scoped fixes locally with a conventional commit message.

## Implementation steps

1. Inspect each failing test against the current PR-monitor command sequence.
2. Add missing faked git results or monkeypatches for current guard calls such
   as mirror anchor checks, clean validation worktree checks, head preservation,
   status refreshes, and sanitized-env runner calls.
3. Re-run the nine focused shard-6 tests.
4. Re-run the line-limit split checks if touched files affect maintainability.
5. Write validation notes and commit the plan, focused fixes, and validation docs.

## Verification commands and pass criteria

- The focused nine-test shard-6 pytest command passes.
- `uv run --python 3.12 --extra dev ruff check <touched files>` passes.
- The shard-8 line-limit guardrail remains passing after the test split.
- Full AWF/GitHub validation and coverage gates remain managed by AWF after
  agent completion.
