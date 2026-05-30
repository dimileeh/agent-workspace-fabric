# PRRT_kwDOSJAM6s6F21l6 Plan

## Problem Statement

The owned-path classifier filters generated AWF planning artifacts under
`docs/awf-plans/`, but it currently recognizes only `ws_*.md` and
`ws_*.conformance.json`. Profiles may configure
`conformance_report_path: docs/awf-plans/{workspace_id}.json`, which produces
generated reports such as `docs/awf-plans/ws_123.json`. Those reports should be
treated as AWF internal artifacts for inter-workspace overlap checks.

## Scope

- Add focused regression coverage for generated `ws_*.json` conformance report
  paths and globs under `docs/awf-plans/`.
- Update the classifier to include the supported plain `.json` report suffix
  without broadening the exception to arbitrary docs paths.
- Preserve existing safety coverage for `docs/awf-plans/**`,
  `docs/awf-plans/README.md`, nested paths, and non-generated filenames.

## Requirements Checklist

- `docs/awf-plans/ws_123.json` is classified as an internal plan artifact.
- `docs/awf-plans/ws_*.json` is filtered out of inter-workspace owned paths.
- Existing generated `ws_*.md` and `ws_*.conformance.json` paths remain
  internal.
- Real documentation paths under or near `docs/awf-plans/` remain ordinary
  owned paths.
- Focused tests and lint cover the touched helper.

## Implementation Steps

1. Add failing tests in `tests/unit/common/test_owned_paths.py` for plain
   `.json` generated report paths and globs.
2. Run the focused owned-path tests to confirm the new regression fails before
   implementation.
3. Extend `src/awf/common/owned_paths.py` to classify generated `ws_*.json`
   filenames as internal artifacts.
4. Re-run focused tests and lint for the helper and test file.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
- Full AWF/GitHub validation is intentionally not run inside the agent phase;
  AWF owns broad validation and merge gating after completion.
