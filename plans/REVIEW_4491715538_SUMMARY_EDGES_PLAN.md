# Review 4491715538 Summary Edges Plan

## Problem Statement and Scope

Address the remaining actionable findings from PR review-level comment
`issue:4491715538` against the protected-file quality-gate classifier.

Scope is limited to `src/awf/control/quality_gates.py`, focused unit
regressions in `tests/unit/control/test_quality_gates.py`, the protected-files
operator documentation, and this plan/validation pair. Existing safety
regressions are policy evidence and must not be weakened.

## Requirements Checklist

- Preserve the existing regression that a `fail_under` change plus another
  `tool.coverage` setting change reports both the threshold violation and the
  separate coverage-policy violation.
- Make non-numeric or absent-side `fail_under` diagnostics precise, avoiding
  the misleading "must remain numeric" wording when the threshold was added or
  removed rather than type-changed.
- Keep reporting other coverage-policy changes when an added, removed, or
  type-changed `fail_under` appears in the same diff.
- Make broad validation command detection quote-aware so separators inside
  quoted strings do not create synthetic shell segments.
- Continue detecting real broad validation commands after shell separators and
  newlines.
- Allow semantically safe `with:` input value updates during pinned action
  version bumps for actions beyond `actions/setup-python`, while continuing to
  block added/removed inputs and sensitive keys/values.
- Keep `docs/PROTECTED_FILES.md` aligned with the expanded pinned-bump
  `with:` allowlist.
- Run focused regression tests, the quality-gate unit file, and lint for the
  touched Python files.
- Commit the scoped changes locally on the current AWF-managed branch without
  pushing or switching branches.

## Implementation Steps

1. Add failing regressions for absent-side `fail_under` messages, quoted `&&`
   in broad validation detection, and safe `actions/cache` input updates during
   a pinned ref bump.
2. Run the new focused tests and confirm they fail where practical.
3. Update coverage violation construction to use clearer reason text and
   explicit other-coverage-policy emission.
4. Update broad validation command detection to tokenize shell commands before
   splitting command segments.
5. Expand the action-specific pinned-bump `with:` allowlist to cover safe
   `actions/cache` cache-key/path value updates.
6. Update `docs/PROTECTED_FILES.md` for the expanded allowlist.
7. Re-run focused tests, the full quality-gate unit file, and ruff.
8. Record validation evidence in
   `plans/REVIEW_4491715538_SUMMARY_EDGES_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "fail_under or broad_validation_command_detection or pinned_uses_version_bump"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.

Any remaining gap must be documented in the validation artifact with the
conflicting safety evidence or defer reason.
