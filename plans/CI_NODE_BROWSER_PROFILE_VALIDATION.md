# CI Node Browser Profile Validation

Plan reference: `plans/CI_NODE_BROWSER_PROFILE_PLAN.md`

## Requirement Status

- Focused CI repro attempted before editing: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q`
    returned one explicit skip because Docker daemon or Compose plugin is not
    available in this AWF agent workspace.
- Preserve AWF workspace contract: Complete.
  - Evidence: no branch switch, no push/rebase, and no full coverage or broad CI
    validation was run locally.
- Add regression coverage for the browser fixture image contract: Complete.
  - Evidence: `tests/unit/fixtures/test_node_next_browser_validator_server.py`
    now asserts `Dockerfile.playwright` does not use the MCR Playwright base
    image and declares the distro Chromium executable-path contract.
- Preserve browser validation semantics with an explicit Chromium executable:
  Complete.
  - Evidence: `browser/validator-server.mjs` passes
    `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to `chromium.launch` when configured,
    and the focused unit test verifies the launch options.
- Run targeted checks: Complete.
  - Evidence:
    - `uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py -q`
      passed with 6 tests.
    - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_workspace_services_compose.py::test_rendered_node_next_browser_compose_expresses_browser_validation_semantics tests/unit/profiles/test_workspace_services_profile.py::test_node_next_browser_workspace_services_profile_preserves_service_schema tests/unit/profiles/test_workspace_services_profile.py::test_node_next_browser_profile_services_resolves_worktree_paths_without_host_ports -q`
      passed with 3 tests.
    - `uv run --python 3.12 --extra dev ruff check tests/unit/fixtures/test_node_next_browser_validator_server.py`
      passed.
    - Re-running the focused integration repro returned the same Docker-unavailable
      skip locally.
- Commit the fix locally: Complete.
  - Evidence: the fix is committed locally as part of this CI-fix cycle; AWF
    owns the later push and PR update.

## Files Changed

- `tests/fixtures/workspace_services/node_next_browser_app/Dockerfile.playwright`
- `tests/fixtures/workspace_services/node_next_browser_app/browser/validator-server.mjs`
- `tests/unit/fixtures/test_node_next_browser_validator_server.py`
- `plans/CI_NODE_BROWSER_PROFILE_PLAN.md`
- `plans/CI_NODE_BROWSER_PROFILE_VALIDATION.md`

## Residual Risk

The Docker-backed integration test could not execute end to end in this agent
container because Docker is unavailable. Full AWF/GitHub validation, including
the full coverage gate that originally failed, remains managed by AWF after
agent completion.
