# Add Redundant PR Creation For Transient GitHub Failures

## Summary

Feature workspaces can validate and push successfully, then fail during
`gh pr create` when GitHub returns a transient GraphQL/network error. The pushed
branch is left for a human to salvage. Add bounded retry and same-repo PR
reconciliation to the executor PR-open path so transient forge failures do not
strand completed work.

## Implementation Plan

- Keep the normal happy path as a single `gh pr create` call.
- Add shared GitHub transient classification for API/CLI errors and reuse it
  from both PR monitor retries and feature PR creation.
- After a transient or duplicate-PR create failure, run a same-repo open-PR
  lookup scoped by head branch and base branch.
- If the lookup finds a matching PR, return it as the PR result and continue the
  existing executor handoff.
- If no PR is found, retry transient create failures with a small exponential
  backoff budget; deterministic errors still fail immediately.
- Include retry/reconciliation metadata in PR audit evidence and final failure
  evidence without changing the downstream executor state machine.

## Validation Plan

- Add focused PR creator tests for transient retry success, transient
  reconciliation, duplicate reconciliation, fork-collision rejection, and
  deterministic no-retry behavior.
- Add/adjust executor tests so PR-create failure evidence carries retry metadata
  when retry exhaustion occurs while existing exact evidence remains unchanged
  for ordinary deterministic failures.
- Run targeted PR creator/executor tests plus `ruff` and `mypy` on touched
  Python files.
