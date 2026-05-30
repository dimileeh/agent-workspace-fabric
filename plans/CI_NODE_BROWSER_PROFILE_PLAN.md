# CI Node Browser Profile Plan

## Problem Statement And Scope

PR #302 fails the `python-full-coverage` CI job in the Docker-backed
`test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up`
integration test. CI reaches the fixture's browser sidecar build and fails while
resolving `mcr.microsoft.com/playwright:v1.49.1-noble` with `403 Forbidden`.

Scope is limited to the node-next-browser fixture and focused regression tests
that keep the browser validation sidecar behavior while removing the brittle MCR
base-image dependency from this test fixture.

## Requirements Checklist

- Reproduce or attempt the focused CI repro command before editing.
- Keep AWF branch/push/validation ownership intact: no branch switching, no push,
  no broad full-suite or coverage validation locally.
- Add regression coverage for the fixture image contract so the browser sidecar
  does not depend on the MCR Playwright base image.
- Preserve real browser validation semantics by launching Playwright against an
  explicit Chromium executable path supplied by the fixture image.
- Run targeted tests that cover the changed fixture behavior.
- Create a validation document with requirement-by-requirement status and
  evidence.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Record the focused repro result and CI log root cause.
2. Add a static regression test for `Dockerfile.playwright` that rejects the MCR
   Playwright base and requires a distro Chromium executable path contract.
3. Add a validator-server regression test proving
   `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` is passed to `chromium.launch`.
4. Update `Dockerfile.playwright` to use a Node/Debian base, install Chromium via
   apt, install `playwright-core`, and set the executable path environment
   variable.
5. Update the validator server to pass the optional executable path to
   Playwright while preserving existing behavior when the variable is absent.
6. Run focused unit tests for the fixture and, where possible, rerun the focused
   integration repro. Note that Docker is unavailable locally if the integration
   test skips.
7. Write `plans/CI_NODE_BROWSER_PROFILE_VALIDATION.md` and commit the changes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q`
  - Pass criterion: passes in a Docker-capable environment; in this AWF agent
    workspace, an explicit Docker-unavailable skip is acceptable evidence.
- `uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py -q`
  - Pass criterion: all fixture unit regressions pass.
- Optional focused profile/compose tests if needed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_workspace_services_compose.py::test_rendered_node_next_browser_compose_expresses_browser_validation_semantics tests/unit/profiles/test_workspace_services_profile.py::test_node_next_browser_workspace_services_profile_preserves_service_schema -q`
  - Pass criterion: profile and rendered compose contracts still pass.

Full AWF/GitHub validation, including the full coverage gate, remains managed by
AWF after agent completion per the workspace contract.
