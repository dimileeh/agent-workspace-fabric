# PRRT_kwDOSJAM6s6F7_Qy Cursor Runtime CLI Plan

## Problem Statement And Scope

The broad provider readiness path currently treats Cursor as ready when `CURSOR_API_KEY` is present, even if the configured agent runtime image does not expose `cursor-agent`. The selected launch preflight already probes the runtime CLI, but doctor/smoke readiness surfaces use `collect_agent_readiness()` and can report a misleading green state.

Scope is limited to Cursor provider readiness in `src/awf/service/provider_readiness.py` and focused unit coverage in the provider readiness tests. No GitHub writes, pushes, branch changes, or broad AWF/GitHub-owned validation will be run in the agent phase.

## Requirements Checklist

- [ ] When Cursor auth is present, broad provider readiness probes the configured agent runtime image for `cursor-agent`.
- [ ] If the Cursor runtime CLI probe fails, the Cursor provider result is not OK and includes a Cursor runtime reason/message/detail without leaking secrets.
- [ ] If Cursor auth is missing, readiness still fails for missing auth and does not probe the runtime CLI.
- [ ] Existing credential source, credential scope, isolation, and warning metadata are preserved in Cursor readiness responses.
- [ ] Selected Cursor launch preflight remains ready when auth, model, and runtime CLI are available.
- [ ] Focused tests cover the broad readiness regression and existing selected-preflight behavior.

## Implementation Steps

1. Add a failing unit test showing `collect_agent_readiness()` blocks or warns Cursor when `CURSOR_API_KEY` exists but `cursor-agent` is missing from the runtime image.
2. Add helper logic that combines `_check_cursor()` with `_probe_agent_runtime_cli()` for the broad provider readiness path.
3. Reuse the existing runtime probe helper and provider-result redaction paths.
4. Update affected tests whose fake subprocess runners currently assume only GitHub probes occur.
5. Run only targeted provider-readiness tests needed for this review thread.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q`

Pass criteria: the focused provider readiness test file passes, and the validation document records that full AWF/GitHub validation is intentionally left to AWF after agent completion.
