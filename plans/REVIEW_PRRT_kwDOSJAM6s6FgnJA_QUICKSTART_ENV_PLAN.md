# Review PRRT_kwDOSJAM6s6FgnJA Quickstart Env Plan

## Problem Statement And Scope

The PR review thread reports that `docs/QUICKSTART.md` tells users in a clean
source checkout to run `awf service bootstrap` after exporting only
`AWF_GITHUB_TOKEN`. The current Compose file requires `AWF_API_TOKEN` and
`AWF_POSTGRES_PASSWORD` during interpolation, and the bootstrap command now reads
existing env sources instead of creating the missing Compose env file first.

Scope is limited to the Quickstart startup instructions and the narrow docs
regression that covers them.

## Requirements Checklist

- Quickstart startup copy-paste path sets the required local service values
  before the first `awf service bootstrap`.
- Quickstart no longer claims that `awf service bootstrap` persists
  Compose-interpolated values into `docker/compose/.env`.
- Regression coverage fails when the Quickstart startup section omits
  `AWF_API_TOKEN` or `AWF_POSTGRES_PASSWORD` before bootstrap.
- Validation remains focused; broad AWF/GitHub validation is left to AWF after
  agent completion.

## Implementation Steps

1. Update the focused public-docs test for the Quickstart startup section to
   require `AWF_API_TOKEN`, `AWF_POSTGRES_PASSWORD`, and `AWF_GITHUB_TOKEN`
   before `awf service bootstrap`.
2. Run that one targeted test and confirm it fails against the current
   Quickstart.
3. Update `docs/QUICKSTART.md` with the required exports and corrected
   env-file wording.
4. Rerun the same targeted test and confirm it passes.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py::test_quickstart_uses_runnable_startup_path -q`
  - First run after the test change should fail because the current Quickstart
    omits required env values.
  - Final run after the docs change should pass.
