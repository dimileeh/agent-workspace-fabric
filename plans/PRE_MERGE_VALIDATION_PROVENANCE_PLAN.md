# Pre-Merge Validation Provenance UX Plan

## Summary

Fix the confusing PR-monitor recovery state where a PR with green GitHub CI and resolved comments reports `VALIDATION_INSUFFICIENT_TIER` before merge. The underlying safety behavior is correct: AWF should require AWF-owned validation provenance for the exact current PR head before auto-merge. The bug is that AWF uses the same reason for true tier insufficiency and missing current-head validation, and validate-only recovery does not consistently record the PR head as the validation target.

## Changes

- Add a distinct stale reason for missing validation on the current PR head, while preserving `validation_insufficient_tier` for actual required-tier gaps.
- Keep validate-only recovery behavior and `monitoring_pr -> ready -> validating -> monitoring_pr` state flow, but make operation reason/message honest.
- Ensure validate-only PR-monitor recovery records the source PR head as `target_head_sha` so validation freshness becomes `fresh` once recovery succeeds.
- Update validation observability and console-facing reason text to avoid implying GitHub CI failed or the validation tier was too low when the real issue is missing AWF-owned current-head evidence.

## Tests

- Merge gate dispatches validate-only recovery with the new current-head reason when tier is sufficient but no validation run matches the current PR head.
- True tier gaps continue to use `VALIDATION_INSUFFICIENT_TIER`.
- Validate-only recovery records `target_head_sha` from the recovery payload source head.
- Validation summary reports fresh after validate-only recovery when the current target head matches the recorded validation target.

## Assumptions

- GitHub CI remains an external merge gate and does not replace AWF-owned validation provenance.
- This change does not alter scheduling, review grace, merge policy, or validation command selection.
