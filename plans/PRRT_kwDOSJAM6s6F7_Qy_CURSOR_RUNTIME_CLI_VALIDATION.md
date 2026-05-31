# PRRT_kwDOSJAM6s6F7_Qy Cursor Runtime CLI Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F7_Qy_CURSOR_RUNTIME_CLI_PLAN.md`

## Requirement Status

- Complete: When Cursor auth is present, broad provider readiness probes the configured agent runtime image for `cursor-agent`.
- Complete: If the Cursor runtime CLI probe fails, the Cursor provider result is not OK and includes a Cursor runtime reason/message/detail without leaking secrets.
- Complete: If Cursor auth is missing, readiness still fails for missing auth and does not probe the runtime CLI.
- Complete: Existing credential source, credential scope, isolation, and warning metadata are preserved in Cursor readiness responses.
- Complete: Selected Cursor launch preflight remains ready when auth, model, and runtime CLI are available.
- Complete: Focused tests cover the broad readiness regression and existing selected-preflight behavior.

## Evidence

Files changed:

- `src/awf/service/provider_readiness.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py`
- `plans/PRRT_kwDOSJAM6s6F7_Qy_CURSOR_RUNTIME_CLI_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F7_Qy_CURSOR_RUNTIME_CLI_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_provider_readiness_cursor_env_auth_requires_runtime_cli -q`
  - Initial TDD run failed before implementation with Cursor reporting `ok=True`.
  - Post-implementation run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q`
  - Passed: 49 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/provider_readiness.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/provider_readiness.py`
  - Passed.

Full AWF/GitHub-owned validation, coverage gates, and CI-equivalent checks were intentionally not run during the agent phase per the workspace contract. AWF will run broad validation and record provenance after agent completion.

## Gaps

No gaps remain against the saved plan.
