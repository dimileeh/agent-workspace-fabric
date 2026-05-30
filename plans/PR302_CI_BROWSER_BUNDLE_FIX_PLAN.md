# PR302 CI Browser Bundle Fix Plan

## Problem Statement And Scope

PR #302 still fails the GitHub Actions `python-full-coverage` job in
`tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up`.
The focused CI log shows setup and both profile healthchecks pass, then
`node scripts/validate-browser.mjs` exits 1 within a few seconds. Earlier fixes
made the browser sidecar non-root-safe and added bounded validation retries, so
the remaining failure is likely a persistent Chromium launch/runtime issue in
the fixture image rather than app readiness.

Scope is limited to the Node/browser fixture image contract and focused
no-Docker regression tests. Do not edit protected CI/workflow files, do not
skip or weaken the Docker integration test, do not switch branches, do not push,
and do not run broad AWF/GitHub-owned validation locally.

## Requirements Checklist

- Keep the browser validation check real: no skipping, disabling, or weakening
  the Docker integration assertion.
- Keep the sidecar off the MCR Playwright base image and keep the runtime
  non-root.
- Replace the fragile distro Chromium runtime with the Playwright package's
  matching bundled Chromium installed during the fixture image build.
- Preserve the optional explicit Chromium executable override for local or
  future profile variants.
- Update focused fixture tests to cover the new browser image contract.
- Run the AWF-provided focused repro first and record that this workspace skips
  it because Docker is unavailable.
- Run focused no-Docker tests and syntax checks for changed fixture behavior.
- Write a validation document with requirement-by-requirement evidence and
  leave full AWF/GitHub validation to AWF after agent completion.
- Commit locally with a conventional `fix(ci): ...` message and do not push.

## Implementation Steps

1. Update `Dockerfile.playwright` to install `playwright@1.49.1`, use
   `playwright install --with-deps chromium`, store browsers in a shared
   `PLAYWRIGHT_BROWSERS_PATH`, and chown that path for the non-root `awf` user.
2. Update the validator server to import Playwright from the installed
   `playwright` package while preserving the existing optional
   `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` launch override.
3. Update fixture unit tests so the Dockerfile contract requires the bundled
   Playwright browser path/install command instead of distro `/usr/bin/chromium`.
4. Run focused fixture/runtime tests and Node syntax checks.
5. Create `plans/PR302_CI_BROWSER_BUNDLE_FIX_VALIDATION.md` with evidence and
   any residual risk.
6. Commit the plan, validation, fixture, and test changes locally.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q
```

Pass criterion: passes in a Docker-capable environment; in this AWF agent
workspace an explicit Docker-unavailable skip is recorded as an environment
limitation.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py tests/unit/runtime/test_node_next_browser_profile_validation.py -q
```

Pass criterion: all focused no-Docker fixture and validation-script regressions
pass.

```bash
node --check tests/fixtures/workspace_services/node_next_browser_app/browser/validator-server.mjs
```

Pass criterion: validator server syntax is valid.

Full `python-full-coverage`, repository coverage gates, and required CI rollup
remain AWF/GitHub-owned validation after this agent phase.
