# CI Node Browser Profile Fix Plan

## Problem Statement And Scope

PR #302 fails the `python-full-coverage` GitHub Actions job because
`tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up`
gets a failed `ValidationResult` after both profile healthchecks pass. The CI
log shows the profile command `node scripts/validate-browser.mjs` exiting 1.

Local focused repro was run first, but this AWF workspace cannot reach a Docker
daemon, so the Docker integration test is skipped locally. The local fix must
therefore use no-Docker regressions around the Node/browser fixture behavior and
avoid broad/full validation.

## Requirements Checklist

- Preserve the CI check; do not skip, disable, or weaken it.
- Keep changes scoped to the Node/browser fixture behavior and matching focused
  regressions.
- Make browser validation resilient to transient first-call readiness failures
  while preserving failed-attempt diagnostics.
- Make the Playwright Chromium launch more CI-safe for constrained Docker
  runners.
- Record focused local validation evidence and defer broad AWF/GitHub
  validation to AWF after agent completion.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Add failing no-Docker unit coverage for transient browser validation failure
   recovery in `scripts/validate-browser.mjs`.
2. Add focused coverage that the browser validator launches Chromium with
   CI-safe shared-memory handling.
3. Implement bounded validation retries with stderr diagnostics in
   `validate-browser.mjs`.
4. Add the Chromium launch flag needed for constrained CI containers.
5. Run the targeted unit tests and the AWF-provided focused repro command.
6. Write `plans/CI_NODE_BROWSER_PROFILE_FIX_VALIDATION.md` with evidence and
   residual risk.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_node_next_browser_profile_validation.py -q`
  - Passes and proves the no-Docker regressions.
- `uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q`
  - In this workspace, may skip because Docker daemon is unavailable; the skip
    is recorded as an environment limitation, not as final proof.

Full AWF/GitHub validation remains owned by AWF after this agent phase.
