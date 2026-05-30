# Comment 4390554359 Non-Root Playwright Fixture Validation

Plan reference: `plans/COMMENT_4390554359_NON_ROOT_PLAYWRIGHT_FIXTURE_PLAN.md`

## Requirement Status

- Complete: Verified the reviewer finding against the current Dockerfile.
  - Evidence: `Dockerfile.playwright` initially had no `USER` directive.
- Complete: Added regression coverage requiring the fixture image to create and
  switch to a non-root user.
  - Evidence: `test_browser_sidecar_dockerfile_runs_validator_as_non_root_user`
    asserts user creation, app ownership, owned copied runtime files, and
    `USER awf` before `CMD`.
- Complete: Updated `Dockerfile.playwright` so `/app` and copied runtime files
  are owned by the non-root user and the validator runs unprivileged.
- Complete: Preserved the existing distro Chromium and Playwright executable
  path contract.
- Complete: Ran focused fixture unit tests and narrow lint for the touched
  Python test file.
- Complete: Did not run AWF/GitHub-owned broad validation; AWF owns that after
  agent completion.

## Evidence

Files changed:

- `tests/fixtures/workspace_services/node_next_browser_app/Dockerfile.playwright`
- `tests/unit/fixtures/test_node_next_browser_validator_server.py`
- `plans/COMMENT_4390554359_NON_ROOT_PLAYWRIGHT_FIXTURE_PLAN.md`
- `plans/COMMENT_4390554359_NON_ROOT_PLAYWRIGHT_FIXTURE_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py::test_browser_sidecar_dockerfile_runs_validator_as_non_root_user -q
```

Initial result before implementation: failed because the Dockerfile had no
`useradd`, owned copy, or `USER awf` contract. Final result: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py -q
```

Final result: `7 passed`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/fixtures/test_node_next_browser_validator_server.py
```

Final result: `All checks passed!`.

## Gaps

No planned gaps remain. Full repository validation, coverage gates, Docker image
builds, and GitHub/AWF merge checks were intentionally not run in this agent
phase per the workspace contract.
