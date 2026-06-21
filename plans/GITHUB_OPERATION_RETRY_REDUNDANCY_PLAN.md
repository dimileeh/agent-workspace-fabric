# GitHub Operation Retry And Redundancy Plan

## Summary

Fix AWF's GitHub retry gap where `gh pr create` can fail with GitHub's
GraphQL `HTTP 400 ... Please try resubmitting` response and be treated as a
deterministic failure. Keep retries bounded and call-site aware so mutating
GitHub operations do not duplicate side effects.

## Implementation

- Classify GitHub API/GraphQL failures containing `try resubmitting` as
  transient, while keeping generic HTTP 400, auth, missing repo, invalid branch,
  and no-commits errors deterministic.
- Preserve the existing feature PR creation retry/reconcile path and add direct
  regression coverage for the malformed GraphQL 400 text.
- Add the same bounded retry and same-repo reconciliation behavior to release PR
  creation, with structured log evidence for attempts, wait times, lookups, and
  exhaustion.
- Rely on the shared classifier for PR monitor GitHub retry helpers; do not add
  blind low-level retries inside `GitHubClient`.

## Validation

- Targeted unit tests for the classifier, feature PR creation, release PR sync,
  PR monitor transient retry, and executor PR-open evidence.
- `ruff` and `mypy` on touched common/runtime files.
