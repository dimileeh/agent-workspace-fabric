# PRRT_kwDOSJAM6s6FL2RB Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FL2RB_PLAN.md`

## Requirement Status

- Complete: Reject dependencies on `agent`, because the rendered agent service
  does not define a healthcheck.
- Complete: Reject dependencies on any profile or companion service without
  `healthcheck_cmd`.
- Complete: Preserve existing unknown-target, name-collision, docker-mode, and
  cycle validation behavior.
- Complete: Add focused regression coverage for unhealthy dependency targets.
- Complete: Avoid broad AWF/GitHub-owned validation; only focused local checks
  were executed.

## Evidence

Changed files:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`

Focused checks:

- Initial TDD check failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  failed because unhealthy dependency targets were not rejected.
- Final targeted unit check passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  passed with `13 passed`.
- Focused lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py`
- Focused type check passed:
  `uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py`

Full AWF/GitHub validation was not run in the agent phase per workspace
contract; AWF owns broad validation and merge-gate provenance after completion.
