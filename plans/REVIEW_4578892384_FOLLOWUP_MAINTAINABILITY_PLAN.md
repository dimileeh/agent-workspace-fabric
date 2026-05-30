# Review 4578892384 Follow-Up Maintainability Plan

## Problem Statement And Scope

Address the remaining review-level maintainability feedback from PR comment
`issue:4578892384`.

Scope is limited to:

- removing a redundant `paths` guard in merge-queue candidate owned-path
  filtering;
- replacing the local staleness wildcard helper with the shared owned-path
  helper;
- documenting why custom-profile workspace-id globs intentionally accept only
  generated workspace-id shapes even though the default AWF plan directory
  accepts broader `ws_*` artifact names.

No merge-safety behavior change is intended.

## Requirements Checklist

- Preserve merge-queue fallback from workspace paths to attempt paths when
  workspace paths are empty or filter entirely to internal plan artifacts.
- Remove the duplicate `_has_wildcard` implementation from
  `src/awf/service/staleness.py`.
- Add an inline comment in `src/awf/common/owned_paths.py` explaining the
  constrained workspace-id glob charset versus the broader default-directory
  filename classifier.
- Avoid broad AWF/GitHub-owned validation; run only focused checks for changed
  files and nearby behavior.

## Implementation Steps

1. Simplify `_candidate_owned_paths()` in `src/awf/service/merge_queue.py` so
   `interworkspace_owned_paths()` handles empty workspace paths directly.
2. Import `_has_wildcard` from `awf.common.owned_paths` in
   `src/awf/service/staleness.py` and delete the local duplicate helper.
3. Add a targeted explanatory comment near `_workspace_id_glob_path_pattern()`
   in `src/awf/common/owned_paths.py`.
4. Run focused formatting/lint and unit checks covering the touched helper
   paths.
5. Record validation evidence in
   `plans/REVIEW_4578892384_FOLLOWUP_MAINTAINABILITY_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/runtime/test_merge_queue_ordering.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py src/awf/service/merge_queue.py src/awf/service/staleness.py`
  passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
