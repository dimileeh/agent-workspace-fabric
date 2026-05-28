# PRRT_kwDOSJAM6s6FYsG Compose Build Budget Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FYsG_COMPOSE_BUILD_BUDGET_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Keep `--wait-timeout` equal to the effective `compose_up_timeout_seconds`. | Complete | Existing and updated subprocess tests assert the CLI still receives `--wait-timeout 900`. |
| Budget Docker Compose build/recreate/start time separately from Compose readiness wait. | Complete | Added `_compose_up_capture_timeout_seconds()` in `src/awf/node/compose_manager.py`; for `wait=True` it now budgets launch timeout + readiness timeout + buffer. |
| Apply the same outer timeout policy to `up()` and `ensure_project_up()`. | Complete | Both entry points now call the shared helper. Regression coverage covers both methods in `tests/unit/node/test_compose_manager_subprocess.py`. |
| Preserve structured `DOCKER_COMMAND_TIMEOUT` failures for genuinely hung compose subprocesses. | Complete | Existing timeout handling is unchanged; full touched test file still passes. |
| Run only focused checks; full AWF/GitHub validation remains post-agent owned. | Complete | Ran only targeted unit and lint checks listed below. |

## TDD Evidence

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager_subprocess.py -q -k "compose_timeout or ensure_project_up"`
  failed with old `960.0` outer timeout where the new expected timeout is `1860.0`.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager_subprocess.py -q -k "compose_timeout or ensure_project_up"`
  passed with `2 passed, 13 deselected`.
- Focused regression file:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager_subprocess.py -q`
  passed with `15 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/compose_manager.py tests/unit/node/test_compose_manager_subprocess.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase per workspace
contract; AWF owns broad validation, provenance, and merge gating after agent
completion.
