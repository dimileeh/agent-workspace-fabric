# PR302 CI Browser Bundle Fix Validation

Plan reference: `plans/PR302_CI_BROWSER_BUNDLE_FIX_PLAN.md`

## Requirement Status

- Complete: Kept the browser validation check real.
  Evidence: no workflow, marker, skip condition, or integration assertion was
  changed. The Docker-backed test still asserts `validation_result.all_passed`
  when Docker is available.
- Complete: Kept the sidecar off the MCR Playwright base image and non-root.
  Evidence:
  `tests/fixtures/workspace_services/node_next_browser_app/Dockerfile.playwright`
  still starts from `node:22-bookworm-slim`, rejects the MCR image contract in
  focused tests, and still ends with `USER awf`.
- Complete: Replaced the fragile distro Chromium runtime with Playwright's
  matching bundled Chromium.
  Evidence: `Dockerfile.playwright` now installs `playwright@1.49.1` with
  `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`, then runs
  `npm --prefix /app/browser exec -- playwright install --with-deps chromium`
  into `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`.
- Complete: Preserved the explicit Chromium executable override.
  Evidence: `browser/validator-server.mjs` still reads
  `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` and passes `executablePath` when set;
  the focused unit test still covers that launch option.
- Complete: Updated focused fixture tests for the new browser image contract.
  Evidence:
  `tests/unit/fixtures/test_node_next_browser_validator_server.py` now requires
  the bundled Playwright browser install path/command and the non-root ownership
  of `/ms-playwright`.
- Complete: Ran focused repro first and recorded the local Docker limitation.
  Evidence: the AWF-provided integration repro was run before editing and
  skipped locally because Docker daemon/Compose plugin is unavailable in this
  agent workspace.
- Complete: Ran focused no-Docker verification.
  Evidence commands are listed below.
- Complete: Left full AWF/GitHub validation to AWF after agent completion.
  Evidence: no full repository suite, full coverage gate, frontend build, push,
  rebase, or branch switch was run locally.

## Evidence

CI inspection:

```bash
gh pr checks 302 --repo dimileeh/aira-agent-workspace-fabric --json name,state,bucket,link,startedAt,completedAt,workflow
```

Result: `python-full-coverage` and dependent `ci-required` failed; other
required jobs passed.

```bash
gh run view 26655829546 --repo dimileeh/aira-agent-workspace-fabric --job 78565616942 --log | rg -C 8 "test_node_next_browser_profile|AssertionError|docker ps|docker volume|FAILED"
```

Result: the single failing test was
`tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up`;
setup and both healthchecks passed, then `node scripts/validate-browser.mjs`
returned 1.

AWF-provided focused repro before editing:

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q
```

Result in this workspace: skipped, `Docker daemon or Compose plugin not
available`.

Focused checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py tests/unit/runtime/test_node_next_browser_profile_validation.py -q
```

Result: `12 passed in 5.18s`.

```bash
node --check tests/fixtures/workspace_services/node_next_browser_app/browser/validator-server.mjs
node --check tests/fixtures/workspace_services/node_next_browser_app/scripts/validate-browser.mjs
```

Result: both exited 0.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/fixtures/test_node_next_browser_validator_server.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_workspace_services_compose.py::test_rendered_node_next_browser_compose_expresses_browser_validation_semantics tests/unit/profiles/test_workspace_services_profile.py::test_node_next_browser_workspace_services_profile_preserves_service_schema tests/unit/profiles/test_workspace_services_profile.py::test_node_next_browser_profile_services_resolves_worktree_paths_without_host_ports -q
```

Result: `3 passed in 0.57s`.

```bash
git diff --check
```

Result: no whitespace errors.

## Residual Risk

The Docker-backed integration test cannot execute end to end in this AWF agent
workspace because Docker is unavailable. The fix targets the CI-only failure by
removing the system Chromium/runtime-version mismatch from the fixture image
while preserving real Playwright browser validation. Full `python-full-coverage`
and `ci-required` validation remain managed by AWF/GitHub after this agent
phase.
