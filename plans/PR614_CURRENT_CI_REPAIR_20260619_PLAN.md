# PR614 Current CI Repair 2026-06-19 Plan

## Problem Statement And Scope

PR #614 has reported failing CI. The latest pushed PR head is
`e42b7a4c7090105ab471871ec76d5b27d5bec746`; older completed CI runs failed on
coverage shards before several follow-up fix commits landed. This cycle must
distinguish stale failed runs from current-head failures, then make the smallest
code or test change needed for any current failure.

## Requirements Checklist

- Inspect GitHub Actions checks for PR #614 at the current PR head.
- Use older failed run logs only as diagnostic context when the current run has
  not completed yet.
- Do not switch branches, push, rebase, or run broad AWF/GitHub-owned
  validation locally.
- If a current-head job fails, reproduce the narrow failing tests locally before
  editing when practical.
- Add or adjust focused regression coverage for any behavior change.
- Record focused verification evidence and leave full AWF/GitHub validation to
  AWF after agent completion.
- Commit any local fix with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Query PR #614 metadata and current-head checks.
2. Inspect logs from the most recent completed failed run to identify likely
   failure classes while the current run is pending.
3. If the current run fails, fetch only the failed job logs and identify the
   smallest affected source/test surface.
4. Run focused local repro commands for the specific failing tests.
5. Patch the source or tests narrowly, preserving existing monitor fail-closed
   behavior and protected-scope gates.
6. Run targeted verification for the changed files/tests.
7. Create the matching validation document with evidence and residual AWF-owned
   validation handoff.

## Verification Commands And Pass Criteria

- `gh pr checks 614 --repo dimileeh/agent-workspace-fabric --json name,state,bucket,link,startedAt,completedAt,workflow`
  - Pass criteria: identify current current-head failures, or confirm current
    CI is still pending/passing.
- Targeted `uv run --python 3.12 --extra dev pytest ... -q` command(s) for any
  current failing tests.
  - Pass criteria: tests that failed on the current PR head pass locally after
    the fix.
- Targeted `uv run --python 3.12 --extra dev ruff check ...` for edited Python
  files/tests when applicable.
  - Pass criteria: no lint issues in touched files.

Full coverage, full Python shard execution, frontend build, release artifacts,
and CI-required gates remain AWF/GitHub-owned after this agent phase.
