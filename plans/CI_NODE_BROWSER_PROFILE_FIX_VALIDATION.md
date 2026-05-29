# CI Node Browser Profile Fix Validation

Plan reference: `plans/CI_NODE_BROWSER_PROFILE_FIX_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Preserve the CI check; do not skip, disable, or weaken it. | Complete | No workflow, skip marker, or CI gate changed. The integration test remains active when Docker is available. |
| Keep changes scoped to the Node/browser fixture behavior and matching focused regressions. | Complete | Changed only the Node/browser fixture scripts, its unit regression file, and plan artifacts. |
| Make browser validation resilient to transient first-call readiness failures while preserving failed-attempt diagnostics. | Complete | `validate-browser.mjs` now performs bounded retries and writes failed-attempt messages to stderr. `test_browser_validate_script_retries_transient_validation_response` covers one transient 500 followed by success. |
| Make the Playwright Chromium launch more CI-safe for constrained Docker runners. | Complete | `validator-server.mjs` now launches Chromium with `--disable-dev-shm-usage`; `test_browser_validator_uses_ci_safe_chromium_launch_flags` covers it. |
| Record focused local validation evidence and defer broad AWF/GitHub validation to AWF after agent completion. | Complete | Focused evidence is listed below. Full coverage and GitHub CI are intentionally not run locally per AWF workspace contract. |
| Commit the fix locally without pushing or switching branches. | Complete | No branch switch or push was performed; the local commit is created after the validation artifact is included. |

## Focused Evidence

- Failing regression confirmation before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_node_next_browser_profile_validation.py -q`
  - Result: `2 failed, 3 passed`; failures were the new transient-validation retry test and the new CI-safe Chromium flag test.
- Unit regression pass after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_node_next_browser_profile_validation.py -q`
  - Result: `5 passed in 1.17s`
- Focused lint:
  - `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_node_next_browser_profile_validation.py`
  - Result: `All checks passed!`
- Node syntax checks:
  - `node --check tests/fixtures/workspace_services/node_next_browser_app/scripts/validate-browser.mjs`
  - `node --check tests/fixtures/workspace_services/node_next_browser_app/browser/validator-server.mjs`
  - Result: both exited 0.
- AWF-provided focused repro:
  - `uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q`
  - Result in this workspace: skipped because Docker daemon/Compose runtime is unavailable locally.

## Residual Risk

The exact Docker integration test could not execute in this AWF workspace
because `/var/run/docker.sock` is unavailable. The no-Docker regressions cover
the failing command's transient response path and the CI-safe Chromium launch
configuration. Full AWF/GitHub validation remains managed by AWF after this
agent phase.
