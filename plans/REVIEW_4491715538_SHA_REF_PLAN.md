# Review 4491715538 SHA Ref Plan

## Problem Statement and Scope

Address the remaining actionable part of review comment `issue:4491715538` for
protected workflow quality gates. The current branch already contains a local
fix and regression coverage for allowing `continue-on-error: true` to be
removed or set to `false` on validation steps, so that subpoint is treated as
stale against this workspace state.

The remaining issue is that `_is_pinned_uses_bump` allows a GitHub Actions
`uses:` ref to move from a 40-character SHA to a mutable major/minor version
tag such as `v4`. That weakens workflow pinning and should be blocked unless
the replacement version is a full semver ref.

## Requirements Checklist

- Preserve the existing allowance for moving from a mutable version tag to a
  pinned SHA.
- Block moving from a pinned SHA to a mutable major/minor version tag.
- Allow moving from a pinned SHA to a full semver version ref.
- Preserve the existing allowance for non-downgrade full-version bumps.
- Keep the `continue-on-error` strictness-improvement behavior unchanged.

## Implementation Steps

1. Add focused regression tests in `tests/unit/control/test_quality_gates.py`
   for SHA to mutable major tag blocking and SHA to full semver allowance.
2. Run the SHA-to-mutable-tag regression test before implementation and confirm
   it fails against current behavior.
3. Update `_is_pinned_uses_bump` in `src/awf/control/quality_gates.py` so
   `new_is_sha` remains allowed, while `old_is_sha` only allows full semver
   replacement refs.
4. Re-run the focused tests and the quality-gate unit module.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "sha_to_mutable_major_tag or sha_to_full_semver"`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
