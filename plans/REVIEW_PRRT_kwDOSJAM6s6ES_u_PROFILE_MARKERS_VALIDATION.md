# Review PRRT_kwDOSJAM6s6ES_u Profile Markers Validation

Plan reference: `plans/REVIEW_PRRT_kwDOSJAM6s6ES_u_PROFILE_MARKERS_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving `awf init <path> --write-profile --yes`
  refuses to create `.awf/workspace.yml` when `.awf/workspace.yaml` already
  exists.
- Complete: Preserved `--force` as the explicit opt-in; the regression asserts
  forced write creates the canonical profile while leaving the alternate marker
  in place.
- Complete: Updated `write_workspace_profile` to consult the shared
  `PROFILE_MARKER_PATHS` list before writing.
- Complete: Kept validation focused. Full AWF/GitHub validation is managed by
  AWF after agent completion.

## Evidence

- Initial failing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k 'alternate_profile_marker'`
  failed because `awf init` wrote `.awf/workspace.yml` beside
  `.awf/workspace.yaml`.
- Passing targeted CLI regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k 'alternate_profile_marker or existing_profile_requires_force'`
  passed with `2 passed, 137 deselected`.
- Passing adjacent shared-writer checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_profile_init.py -q -k 'write_creates or refuses_to_overwrite'`
  passed with `2 passed, 2 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/profiles/onboarding.py tests/unit/cli/test_init.py`
  passed.
- Focused format check:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/profiles/onboarding.py tests/unit/cli/test_init.py`
  passed.
