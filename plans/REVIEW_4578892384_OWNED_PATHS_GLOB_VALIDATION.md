# Review 4578892384 Owned Paths Glob Validation

Plan reference: `plans/REVIEW_4578892384_OWNED_PATHS_GLOB_PLAN.md`

## Requirement Status

- Add a focused regression test proving workspace-id matching follows a changed
  `_WORKSPACE_ID_GLOB` prefix: Complete.
- Update `_workspace_id_glob_path_matches()` to derive the regex prefix from
  `_WORKSPACE_ID_GLOB`: Complete.
- Add the requested inline comment documenting that standalone `/**` entries do
  not perform recursive sub-path matching: Complete.
- Run only targeted checks for the changed owned-path helper: Complete.
- Do not run AWF/GitHub-owned broad validation: Complete.

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `plans/REVIEW_4578892384_OWNED_PATHS_GLOB_PLAN.md`
- `plans/REVIEW_4578892384_OWNED_PATHS_GLOB_VALIDATION.md`

Pre-implementation regression check:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  failed on
  `test_workspace_id_glob_matching_uses_configured_glob_prefix`, confirming the
  hardcoded `ws_` prefix issue.

Post-implementation focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  passed: 24 tests passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, and merge gating after completion.
