# Owned Paths Review 4578892384 Plan

## Problem Statement And Scope

Address the review-level feedback from PR comment `issue:4578892384` for
`src/awf/common/owned_paths.py`.

The actionable scope is limited to the shared owned-path classifier:

- prevent multi-placeholder custom planning artifact globs from matching paths
  that contain different workspace IDs in different `{workspace_id}` positions;
- allow generated custom planning artifact parent scopes ending in `/**` to
  classify concrete workspace-owned parent-scope declarations as internal
  artifacts;
- preserve existing behavior that real documentation paths and non-generated
  plan-adjacent files remain inter-workspace owned.

The staleness behavioral note in the comment is informational. No additional
staleness code change is planned unless the classifier changes reveal a gap.

## Requirements Checklist

- Add regression coverage showing mismatched IDs in a two-placeholder custom
  artifact path are not filtered.
- Add regression coverage showing same-ID two-placeholder custom artifact paths
  are still filtered.
- Add regression coverage showing custom parent `/**` artifact scopes filter
  concrete workspace-ID parent-scope declarations while configured leaf
  artifact paths remain responsible for child-file classification.
- Keep invalid or real documentation paths inter-workspace owned.
- Avoid broad AWF/GitHub-owned validation; run only focused unit tests for the
  changed classifier.

## Implementation Steps

1. Update `tests/unit/common/test_owned_paths.py` with failing regression cases
   for the two review issues.
2. Run the focused test module and confirm the new tests fail against the
   current implementation when practical.
3. Update `src/awf/common/owned_paths.py` so workspace-id globs use one
   captured ID reused for every occurrence in the same configured path.
4. Update configured `/**` artifact-scope matching so workspace-id wildcard
   parent scopes can match concrete workspace-ID scopes and subpaths without
   weakening real-doc boundary cases.
5. Re-run the focused unit test module.
6. Record validation evidence in
   `plans/OWNED_PATHS_REVIEW_4578892384_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
