# PRRT_kwDOSJAM6s6L7DNR Plan

## Problem Statement and Scope

The review thread reports that `Settings.auto_cleanup_orphans` defaults to
`True`, but the local-service compose stack overrides that default by injecting
`AWF_AUTO_CLEANUP_ORPHANS: ${AWF_AUTO_CLEANUP_ORPHANS:-false}` into the API and
worker containers.

Scope is limited to making the canonical local-service compose fallback match
the application default while preserving the operator kill-switch.

## Requirements

- [ ] Verify the review claim against `src/awf/common/config.py` and
  `docker/compose/local-service.yml`.
- [ ] Update the existing compose regression test before changing the compose
  file.
- [ ] Change only the local-service compose fallback needed for this thread.
- [ ] Run focused validation for the changed behavior.
- [ ] Record validation evidence and note that broad AWF/GitHub validation is
  managed after agent completion.

## Implementation Steps

1. Update the canonical compose test expectation for
   `AWF_AUTO_CLEANUP_ORPHANS` from `false` to `true`.
2. Run the focused test to confirm it fails against the current compose file.
3. Update `docker/compose/local-service.yml` to default
   `AWF_AUTO_CLEANUP_ORPHANS` to `true`.
4. Re-run the same focused test.
5. Create the validation artifact and commit the scoped changes.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py::test_local_service_compose_has_expected_service_contract -q`

Pass criteria: the focused compose contract test passes after the compose
fallback is updated. Full AWF/GitHub validation is intentionally left to AWF
after agent completion per workspace contract.
