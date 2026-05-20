# Review 4491715538 Classifier Edges Plan

## Problem Statement And Scope

Address the actionable protected quality-gate classifier feedback from PR review
comment `issue:4491715538`.

Scope is limited to `src/awf/control/quality_gates.py` and focused unit
regressions in `tests/unit/control/test_quality_gates.py`.

Some review text conflicts with existing safety assertions. This plan preserves
the existing fail-closed policy for `write-all`, `body-path`, arbitrary
`steps.<id>.outputs.<key>` expressions, and coverage `fail_under` raises unless
the code can prove a narrower safe case.

## Requirements Checklist

- Allow added informational jobs to use string-form `permissions: read-all`.
- Preserve the existing block for string-form `permissions: write-all`.
- Allow unambiguous same-label prerelease bumps such as `rc1` to `rc2`.
- Preserve prerelease downgrade blocking such as `rc10` to `rc2`.
- Allow safe `peter-evans/create-or-update-comment` input
  `reactions-edit-mode`.
- Preserve the existing block for `body-path` because it can make the action
  read arbitrary workspace file content into a PR comment.
- Keep arbitrary step output expressions blocked in informational/comment text.
- Keep coverage `fail_under` raises blocked without protected-file ownership.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add failing unit regressions for `read-all`, same-label RC bumps, and
   `reactions-edit-mode`.
2. Update informational job permission handling to accept `read-all` only.
3. Update prerelease comparison to treat simple letter-prefix numeric suffixes
   as ordered prerelease identifiers while retaining fail-closed mixed labels.
4. Add `reactions-edit-mode` to the comment action input allowlist.
5. Run targeted tests to prove the new regressions fail before implementation
   and pass afterward, then run the focused quality-gate test module and lint.
6. Record validation in
   `plans/review_4491715538_classifier_edges_VALIDATION.md`.

## Verification Commands And Pass Criteria

- Targeted new tests fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
