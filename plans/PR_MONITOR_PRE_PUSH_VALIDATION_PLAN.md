# PR Monitor Pre-Push Validation Plan

## Summary

PR #288 already records validation provenance for the current PR head and falls
back to validate-only recovery when that provenance is missing. The remaining
gap is that PR-monitor-authored repair commits can be pushed before AWF
validation runs. This change validates monitor repair commits locally before
any push, so a green GitHub PR with resolved comments can merge without a
post-green `monitoring_pr -> ready -> validating` bounce.

## Implementation

- Wire the service `ValidationRunner` into feature and release PR monitors and
  store it on `_RunnerDeps`.
- Add a focused pre-push validation helper for monitor repair paths:
  - capture local `HEAD`;
  - resolve the workspace profile and validation tier;
  - create a validation run with `workspace_head_sha` and `target_head_sha`
    both set to the local head intended for push;
  - run profile `post_agent` and `validate` phases plus existing local coverage
    final-gate policy;
  - finish the validation run with real command/coverage evidence.
- Add a bounded validation-fix pass before push. If validation fails, the
  monitor asks its adapter to fix the validation failure, commits any resulting
  dirty worktree changes through the existing monitor commit helper, then
  validates the new head once more before pushing.
- Route comment repair, CI repair, and sync-base repair pushes through a
  validated push helper. Preserve protected-scope checks before validation and
  preserve comment thread resolution only after a successful push.
- Keep PR #288's missing-current-head validation gate as a fallback for
  externally pushed or otherwise unvalidated PR heads.

## Tests

- Add regression coverage proving monitor repair paths validate before pushing.
- Cover validation failure blocking push and preserving unresolved review
  thread state.
- Cover a successful bounded validation-fix pass before push.
- Cover that validation run provenance records `target_head_sha` for the local
  head that is pushed.
- Re-run focused PR monitor and executor suites, plus ruff/mypy before pushing.
