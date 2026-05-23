# Review PRRT_kwDOSJAM6s6ES_u Profile Markers Plan

## Problem Statement

The PR review thread reports that `awf init <path> --write-profile --yes` only
blocks writes when `.awf/workspace.yml` already exists, while profile discovery
also treats `.awf/workspace.yaml`, `awf.workspace.yml`, and
`awf.workspace.yaml` as existing project profiles. Writing the canonical profile
beside one of those alternate markers can change profile precedence without an
explicit `--force`.

## Requirements

- Add a regression test proving `awf init <path> --write-profile --yes` refuses
  to create `.awf/workspace.yml` when an alternate supported profile marker
  already exists.
- Preserve `--force` as the explicit opt-in for writing the canonical profile
  in that situation.
- Reuse the shared profile marker path list so write behavior stays aligned
  with discovery.
- Keep validation focused to the changed behavior; full AWF/GitHub validation is
  handled after agent completion.

## Implementation Steps

1. Add the focused CLI regression test in `tests/unit/cli/test_init.py`.
2. Run that test and confirm it fails against the current implementation.
3. Update `write_workspace_profile` to check all supported profile marker paths
   before writing when `force` is false.
4. Re-run the focused regression and any immediately adjacent focused test that
   proves normal forced overwrite behavior.
5. Commit the plan, code, tests, and validation evidence locally.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k 'alternate_profile_marker or existing_profile_requires_force'`
- Pass criteria: the alternate-marker case blocks without `--force`, allows with
  `--force`, and existing canonical overwrite behavior remains covered.
