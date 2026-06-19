# PR614 Current Head CI Repair Plan

## Problem Statement

PR #614 has failing CI on head `a6118b146bcfa93618213125e742e6082254f459`.
The current `lint-and-type` job failed during dependency installation with a
PyPI broken-pipe fetch for `pluggy-1.6.0` metadata. A recent completed run on
the same PR branch also failed `python-coverage-shards (8)`, which is a real
test-surface failure candidate and must be inspected before changing code.

## Scope

- Inspect current and recent PR #614 GitHub Actions logs with `gh`.
- Treat external dependency transport failures as evidence, not as a code bug,
  unless a repo-side dependency declaration is clearly causing avoidable drift.
- Diagnose shard 8 failure logs and make the smallest code or test change needed
  for that behavior.
- Do not switch branches, push, rebase, or edit protected workflow files.
- Do not run broad AWF/GitHub-owned validation or full coverage locally.

## Requirements Checklist

- [x] Identify concrete failing check names, run URLs, and root causes.
- [x] If the root cause is a behavior regression, add or adjust focused tests.
- [x] Implement only the minimal fix for the confirmed root cause.
- [x] Run focused local verification for the touched behavior.
- [x] Create `plans/PR614_CURRENT_HEAD_CI_REPAIR_VALIDATION.md` with evidence.
- [x] Commit the local fix with a conventional commit message.

## Implementation Steps

1. Pull logs for the current `lint-and-type` failure and the prior shard 8
   failure.
2. For shard 8, identify the failing test(s) and relevant source/test files.
3. Reproduce those test(s) locally with a narrow `uv run ... pytest <nodeid>`
   command.
4. Apply a scoped source or test change, preferring existing PR monitor test
   patterns.
5. Re-run only the focused test(s) and any narrow lint/type command justified by
   the edited files.
6. Record validation and commit all files changed for this repair.

## Verification Commands and Pass Criteria

- `gh api /repos/dimileeh/agent-workspace-fabric/actions/jobs/<job_id>/logs`
  captures the actionable CI failure snippet.
- Focused pytest node(s) for any failing shard 8 test pass locally.
- If source files are edited, focused ruff for those files passes.
- Full AWF/GitHub validation remains owned by AWF after this agent phase.
