# Address PRRT_kwDOSJAM6s6GUtl_ Validation

Plan reference: `ADDRESS_THREAD_PRRT_kwDOSJAM6s6GUtl__PLAN.md`

## Requirement Status

- Add a regression for a legacy inline `requested_profile` with service host
  ports and no `resolved_profile`: Complete.
- Ensure retry admission includes those requested-profile service ports only
  when the source has no resolved profile snapshot: Complete.
- Preserve existing resolved-profile precedence for modern sources: Complete.
- Keep validation focused; broad AWF/GitHub validation remains managed after
  agent completion: Complete.

## Evidence

Files changed:

- `src/awf/service/workspaces_retry.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GUtl__PLAN.md`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GUtl__VALIDATION.md`

Focused checks:

- Initial regression confirmation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q -k requested_profile`
  failed before implementation because `WorkspaceRetrySourceRuntimeNotReleasedError`
  was not raised.
- Post-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q -k requested_profile`
  passed with `1 passed, 19 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry_port.py`
  passed.

Full AWF/GitHub validation was not run during the agent phase per the workspace
contract; AWF manages broad validation, provenance, and merge gating after agent
completion.
