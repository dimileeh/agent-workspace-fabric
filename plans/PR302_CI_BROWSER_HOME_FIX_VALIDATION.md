# PR302 CI Browser Home Fix Validation

Plan reference: `plans/PR302_CI_BROWSER_HOME_FIX_PLAN.md`

## Requirement Status

- Complete: Inspected PR #302 check status and CI logs before editing.
  Evidence: `gh pr checks 302 --json ...` showed `python-full-coverage`
  failed and `ci-required` failed only because `python-full-coverage` failed.
  `gh run view 26647422311 --job 78536752687 --log` showed the only failing
  test was
  `tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up`;
  both profile healthchecks passed before `node scripts/validate-browser.mjs`
  returned 1.
- Complete: Kept AWF branch and validation ownership intact.
  Evidence: no branch switch, no push, no full-suite or full-coverage local
  validation. Only focused fixture and single-test repro commands were run.
- Complete: Added a focused failing regression for writable non-root browser
  home/cache/config paths.
  Evidence:
  `tests/unit/fixtures/test_node_next_browser_validator_server.py::test_browser_sidecar_dockerfile_runs_validator_as_non_root_user`
  failed before the Dockerfile update on the missing
  `/home/awf/.cache`/`.config` contract.
- Complete: Updated `Dockerfile.playwright` without weakening the existing
  browser fixture contract.
  Evidence:
  `tests/fixtures/workspace_services/node_next_browser_app/Dockerfile.playwright`
  still uses `node:22-bookworm-slim`, distro `chromium`,
  `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium`, and `USER awf`, and
  now creates/chowns `/home/awf/.cache` and `/home/awf/.config` while exporting
  `HOME`, `XDG_CACHE_HOME`, and `XDG_CONFIG_HOME`.
- Complete: Ran focused verification.
  Evidence commands are listed below.
- Complete: Full AWF/GitHub validation remains delegated to AWF after agent
  completion per the workspace contract.

## Verification Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py::test_browser_sidecar_dockerfile_runs_validator_as_non_root_user -q
```

Result before implementation: failed on the missing
`mkdir -p /home/awf/.cache /home/awf/.config` assertion.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py -q
```

Result after implementation: passed, `7 passed in 4.35s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/fixtures/test_node_next_browser_validator_server.py
```

Result after implementation: passed, `All checks passed!`.

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q
```

Result in this AWF workspace: skipped, `Docker daemon or Compose plugin not
available`. This is expected for the local agent phase; the Docker-backed
coverage job is owned by AWF/GitHub CI after completion.

## Residual Risk

The GitHub Actions log does not include the browser validator response body or
container stderr from the failing validation request, so the exact Chromium
launch stderr was not visible from CI logs. The timing and previous change set
point to a non-root browser runtime-state path issue: healthchecks passed, then
the browser validation endpoint returned failure immediately when Chromium
would launch. The added static contract now preserves writable non-root
home/cache/config paths for that runtime.
