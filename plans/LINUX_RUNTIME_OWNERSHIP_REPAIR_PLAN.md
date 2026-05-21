# Linux Runtime Ownership Repair Plan

## Summary

AWF already repairs Linux workspace ownership for the initial worktree and
agent-writable Git metadata. The bug is timing: PR monitor and recovery paths
can create root-owned runtime files such as `.venv` after provisioning, then
structured setup runs `uv sync --extra dev` before another ownership repair.

The fix is to make agent runtime ownership repair a pre-command invariant for
workspace setup and PR-monitor commit cycles, while preserving the existing
Docker Desktop-safe mirror object policy.

## Implementation

- Add a runtime ownership repair helper around
  `repair_agent_writable_worktree(None, worktree_path)`.
- Call it before profile `setup` / `pre_agent` phases in normal execution and
  validate-only PR-monitor recovery.
- Keep the existing Git writability preflight after setup as a Git-specific
  object/ref/status check.
- Run the same repair before and after PR monitor host-side dirty-worktree
  commits, because pre-commit hooks can invoke `uv` as the root control-plane
  user.
- Fail closed with `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED` when ownership
  repair itself fails.
- Document the reason code and make the `.venv` coverage explicit in tests.

## Test Plan

- Add unit coverage proving recursive per-worktree repair includes `.venv/bin`.
- Add executor recovery tests proving ownership repair happens before setup and
  setup is skipped when repair fails.
- Add PR monitor tests proving dirty-worktree commits repair before and after
  Git/pre-commit activity.
- Run focused tests, then ruff, mypy, and the unit test suite.

## Operational Validation

- Rebuild and restart AWF with the local service bootstrap path.
- Reattach PR #270 with Codex `gpt-5.5` and `xhigh` effort.
- Confirm `uv sync --extra dev` succeeds in the new monitor workspace and AWF
  service readiness is green.
