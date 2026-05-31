# PRRT_kwDOSJAM6s6F7_Qv Cursor Agent Home Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F7_Qv_CURSOR_AGENT_HOME_PLAN.md`

## Requirement Status

- Confirm the Dockerfile contract test fails when it requires a shared Cursor
  install prefix and non-root runtime validation: Complete.
- Install Cursor Agent under a shared, readable/traversable prefix instead of
  root's home: Complete.
- Link `/usr/local/bin/cursor-agent` to the shared Cursor Agent entrypoint:
  Complete.
- Validate `cursor-agent` as the `agent` user without masking failures:
  Complete.
- Run focused verification only; do not run broad AWF/GitHub-owned validation:
  Complete.
- Commit the local fix on the current AWF-managed branch: Complete.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PRRT_kwDOSJAM6s6F7_Qv_CURSOR_AGENT_HOME_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F7_Qv_CURSOR_AGENT_HOME_VALIDATION.md`

Focused checks:

- Before the Dockerfile change,
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  failed because the Dockerfile did not install Cursor Agent under
  `/opt/cursor`.
- After the Dockerfile change,
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run in this agent phase.
