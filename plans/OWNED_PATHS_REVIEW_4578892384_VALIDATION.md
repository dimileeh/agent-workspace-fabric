# Owned Paths Review 4578892384 Validation

Plan reference: `plans/OWNED_PATHS_REVIEW_4578892384_PLAN.md`

## Requirement Status

- Complete: Add regression coverage showing mismatched IDs in a
  two-placeholder custom artifact path are not filtered.
- Complete: Add regression coverage showing same-ID two-placeholder custom
  artifact paths are still filtered.
- Complete: Add regression coverage showing custom parent `/**` artifact
  scopes filter concrete workspace-ID parent-scope declarations while
  configured leaf artifact paths remain responsible for child-file
  classification.
- Complete: Keep invalid or real documentation paths inter-workspace owned.
- Complete: Avoid broad AWF/GitHub-owned validation; only focused checks were
  run in the agent phase.

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `plans/OWNED_PATHS_REVIEW_4578892384_PLAN.md`
- `plans/OWNED_PATHS_REVIEW_4578892384_VALIDATION.md`

Focused verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  - Initial TDD run failed with the new regressions:
    `test_repeated_workspace_id_placeholders_require_the_same_id` and
    `test_custom_profile_parent_scope_filters_concrete_workspace_scope`.
  - Final run passed: `39 passed in 0.43s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase. AWF owns broad
validation, provenance, logs, and merge gating after completion.

## Gaps

No planned requirements remain partial or missing.
