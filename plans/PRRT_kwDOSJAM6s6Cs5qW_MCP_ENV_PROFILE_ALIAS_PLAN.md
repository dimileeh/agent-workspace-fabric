# MCP env_profile Alias Review Fix Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6Cs5qW` reports that the collapsed
`awf_create_workspace` MCP tool no longer accepts the legacy `env_profile`
argument, even though REST flat compatibility still maps `env_profile` to
`workspace.profile_ref`. Legacy MCP callers selecting a named profile may fail
schema validation or silently fall back to `auto`.

## Scope

- Preserve the legacy MCP `env_profile` alias for `awf_create_workspace`.
- Keep `requires_database=True` as the higher-priority legacy shortcut mapping
  to profile `aira`.
- Avoid changing unrelated REST, service, or profile resolution behavior.

## Requirements Checklist

- Add a regression test showing an MCP `env_profile` argument persists as the
  workspace `profile_ref`.
- Add MCP tool handling that maps `env_profile` to `profile_ref`.
- Reject conflicting explicit `profile_ref` and `env_profile` values instead of
  guessing.
- Verify the focused MCP test surface passes.

## Implementation Steps

1. Add a unit regression in `tests/unit/mcp/test_mcp_server.py` near the existing
   legacy create-argument tests.
2. Run the focused test and confirm it fails against the current implementation.
3. Add an `env_profile` parameter to `awf_create_workspace` in
   `src/awf/mcp/server.py`.
4. Compute the effective profile in MCP using `requires_database`,
   `env_profile`, and `profile_ref`, with a structured invalid-request response
   for conflicting aliases.
5. Re-run the focused tests and a narrow MCP contract test if needed.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py -q -k "legacy_flat_arguments or env_profile"`

Pass criteria: the new regression fails before implementation, then passes with
the focused MCP compatibility tests after implementation.
