# Comment 4390554359 Non-Root Playwright Fixture Plan

## Problem Statement and Scope

PR review comment `4390554359` reports that
`tests/fixtures/workspace_services/node_next_browser_app/Dockerfile.playwright`
runs the browser validator fixture as root because it has no `USER` directive.

Scope is limited to the Node browser validation fixture Dockerfile and focused
static regression coverage for that Dockerfile contract.

## Requirements Checklist

- Verify the reviewer finding against the current Dockerfile.
- Add regression coverage requiring the fixture image to create and switch to a
  non-root user.
- Update `Dockerfile.playwright` so `/app` and copied runtime files are owned by
  the non-root user and `validator-server.mjs` runs unprivileged.
- Preserve the existing distro Chromium and Playwright executable path contract.
- Run focused tests for `tests/unit/fixtures/test_node_next_browser_validator_server.py`.
- Do not run AWF/GitHub-owned broad validation; record that AWF handles the full
  validation surface after agent completion.

## Implementation Steps

1. Extend the existing Dockerfile static contract test to require a non-root
   user creation/chown step and a `USER awf` directive before `CMD`.
2. Confirm the focused test fails before the Dockerfile change where practical.
3. Update `Dockerfile.playwright` to create the unprivileged user, ensure `/app`
   is writable/readable by that user, copy fixture files with matching ownership,
   and switch users before the command.
4. Re-run the focused fixture unit tests.
5. Create `plans/COMMENT_4390554359_NON_ROOT_PLAYWRIGHT_FIXTURE_VALIDATION.md`
   with requirement-by-requirement evidence.
6. Commit the scoped changes locally with a conventional commit message.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py::test_browser_sidecar_dockerfile_uses_distro_chromium_contract -q
```

Pass criteria: the static Dockerfile contract test passes and enforces the
non-root runtime directive.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/fixtures/test_node_next_browser_validator_server.py -q
```

Pass criteria: all focused Node browser validator fixture unit tests pass. Full
AWF/GitHub validation is intentionally left to AWF after the agent phase.
