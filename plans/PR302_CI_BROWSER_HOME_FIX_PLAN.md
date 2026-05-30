# PR302 CI Browser Home Fix Plan

## Problem Statement And Scope

PR #302 currently fails the GitHub Actions `python-full-coverage` job. The
only failing test is the Docker-backed
`test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up`
integration test. CI shows both profile healthchecks pass, then the browser
validation phase fails with `node scripts/validate-browser.mjs` returning 1.
The dependent `ci-required` job fails only because `python-full-coverage`
failed.

The recent browser fixture changes run the Playwright validator as the non-root
`awf` user. The likely missing container contract is a writable browser home
and XDG cache/config location for Chromium after switching away from root.
Scope is limited to the node-next-browser fixture Dockerfile and focused static
regression coverage for that contract.

## Requirements Checklist

- Inspect PR #302 check status and CI logs before editing.
- Keep AWF branch and validation ownership intact: do not switch branches, do
  not push, and do not run full-suite or full-coverage validation locally.
- Add a focused failing regression that requires the browser sidecar image to
  expose writable non-root home/cache/config paths.
- Update `Dockerfile.playwright` without reintroducing the MCR Playwright base
  image or root runtime.
- Run focused tests covering the changed fixture contract.
- Create a validation document with requirement-by-requirement evidence.
- Commit locally with a conventional CI-fix message.

## Implementation Steps

1. Extend the existing browser sidecar Dockerfile contract test to require
   `HOME`, `XDG_CACHE_HOME`, and `XDG_CONFIG_HOME` under `/home/awf`.
2. Run that focused test to confirm it fails against the current Dockerfile.
3. Update `tests/fixtures/workspace_services/node_next_browser_app/Dockerfile.playwright`
   to create the non-root browser home/cache/config directories, chown them,
   and export the matching environment variables.
4. Re-run the focused fixture unit tests and the single integration repro. The
   integration repro may skip locally when Docker is unavailable in the AWF
   agent workspace.
5. Write `plans/PR302_CI_BROWSER_HOME_FIX_VALIDATION.md` and commit the scoped
   changes.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py::test_browser_sidecar_dockerfile_runs_validator_as_non_root_user -q
```

Pass criterion: fails before the Dockerfile change, then passes after the
Dockerfile declares writable non-root browser home/cache/config paths.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py -q
```

Pass criterion: all focused fixture contract tests pass.

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_node_next_browser_profile_compose.py::test_node_next_browser_profile_runs_setup_health_validate_and_cleans_up -q
```

Pass criterion: passes in a Docker-capable environment; an explicit
Docker-unavailable skip is acceptable in this AWF agent workspace. Full
AWF/GitHub validation, including the full coverage gate, remains managed by AWF
after agent completion.
