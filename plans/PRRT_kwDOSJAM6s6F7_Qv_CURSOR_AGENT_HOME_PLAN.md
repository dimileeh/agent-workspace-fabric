# PRRT_kwDOSJAM6s6F7_Qv Cursor Agent Home Plan

## Problem Statement And Scope

The review thread reports that the Cursor installer runs as root and installs
its versioned files under root's home, while the final runtime runs as the
non-root `agent` user. Scope is limited to the agent-runtime Dockerfile and its
focused Dockerfile contract test.

## Requirements Checklist

- Confirm the Dockerfile contract test fails when it requires a shared Cursor
  install prefix and non-root runtime validation.
- Install Cursor Agent under a shared, readable/traversable prefix instead of
  root's home.
- Link `/usr/local/bin/cursor-agent` to the shared Cursor Agent entrypoint.
- Validate `cursor-agent` as the `agent` user without masking failures.
- Run focused verification only; do not run broad AWF/GitHub-owned validation.
- Commit the local fix on the current AWF-managed branch.

## Implementation Steps

1. Update the focused Dockerfile unit test to require the shared Cursor prefix
   and non-root validation contract.
2. Run that focused test and confirm the current Dockerfile fails it.
3. Update `docker/agent-runtime.Dockerfile` to install Cursor Agent with
   `HOME=/opt/cursor`, make the prefix readable/traversable, link the shared
   entrypoint from `/usr/local/bin`, and add an unmasked check after `USER agent`.
4. Re-run the focused unit test.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6F7_Qv_CURSOR_AGENT_HOME_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Fails before the Dockerfile change for the new shared-prefix/non-root
    validation assertions.
  - Passes after the Dockerfile change.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
