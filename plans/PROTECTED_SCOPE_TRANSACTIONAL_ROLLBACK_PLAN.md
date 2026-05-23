# Protected-Scope Transactional Rollback Plan

## Summary

PR monitor repairs must not leave a PR branch half-mutated when an agent tries to
fix CI or review feedback by editing protected workflow, quality-gate, or config
files outside the workspace scope. If a repair attempt introduces protected-scope
commits, AWF should roll back the entire local repair delta before returning a
blocked result, so no collateral tests, docs, or plans can be pushed without the
protected change they depend on.

## Implementation

- Capture the PR head/operation start SHA before CI repair and comment repair
  agents run.
- Replace committed protected-scope "agent repair" with transactional rollback:
  record attempted protected paths, list files changed since the operation start
  SHA, reset the local worktree to the operation start SHA, clean untracked
  repair leftovers, and return a failed protected-scope push result without
  pushing.
- Keep review/comment addressed-state publish-dependent: if protected rollback
  blocks the push, do not mark feedback fixed or resolve GitHub threads.
- Add audit and monitor-log evidence for protected rollback, including start SHA,
  attempted SHA, protected paths, reverted paths, rollback strategy, and pushed
  false.
- Harden CI and review repair prompts with generic protected-file deferral
  guidance. Do not add Python-specific wording.

## Tests

- Unit test rollback of a repair delta that includes a protected workflow file
  and collateral files, proving no push or second agent repair happens.
- Unit test CI execution records rollback evidence and fails terminally instead
  of partially pushing protected-scope collateral.
- Unit test comment repair clears addressed state and records no fixed feedback
  when protected rollback blocks publication.
- Prompt unit tests verify generic protected-file deferral instructions appear
  in CI and review prompts without Python-specific wording.

## Validation

- Run targeted PR monitor runner tests and monitor prompt tests.
- Run ruff and mypy on touched Python files.
- Leave full coverage and whole-repo validation to GitHub CI/AWF.
