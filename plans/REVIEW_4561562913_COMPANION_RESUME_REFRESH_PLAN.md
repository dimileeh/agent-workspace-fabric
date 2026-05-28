# Review 4561562913 Companion Resume Refresh Plan

## Problem Statement

Address the review-level feedback for PR comment `issue:4561562913` around
PR-monitor resume handling for companion `environment_secrets`.

## Scope

- Limit changes to companion env-secret resume refresh behavior, tests, and
  plan/validation artifacts.
- Preserve the existing launch-time policy that optional env-backed secrets are
  considered present when their source key exists, even if the value is empty.
- Do not persist raw secret values in compose files, logs, tests, or metadata.
- Do not change protected workflow, quality-gate, or broad validation
  configuration files.

## Requirements Checklist

- Add resume-side regression coverage documenting that optional empty source
  environment variables are preserved as present placeholders.
- Add a regression that an optional-secret resume refresh logs a warning when it
  rewrites the persisted compose file through the PyYAML round trip.
- Implement the smallest code change needed for the warning without changing
  compose interpolation preservation.
- Run only focused tests and lint/type checks for the touched files.

## Implementation Steps

1. Add focused tests in the PR-monitor companion-secret executor coverage file.
2. Confirm the warning regression fails before implementation.
3. Add a structured warning after a successful refresh rewrite.
4. Re-run the focused tests and file-scoped lint, then record evidence in a
   validation artifact.
