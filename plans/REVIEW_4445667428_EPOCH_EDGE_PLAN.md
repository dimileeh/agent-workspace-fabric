# Review 4445667428 Epoch Edge Plan

## Problem Statement And Scope

Address the remaining Greptile review observations from PR comment
`issue:4445667428` in failure causality epoch reset detection.

Scope is limited to `src/awf/service/failure_causality.py` and focused unit
coverage in `tests/unit/service/test_failure_causality.py`.

## Requirements Checklist

- Keep the AWF current-branch workflow intact; do not switch branches or push.
- Add regression coverage before the production change.
- Treat `provisioning` as a failure epoch reset state.
- Detect `workspace.remonitor_requested` epoch resets from
  `payload.state_reset.to` instead of depending on the event row `new_state`.
- Preserve existing state-change reset behavior and primary failure causality
  payload semantics.
- Run narrow validation for the touched failure causality behavior.
- Commit local changes with a conventional commit referencing review comment
  `4445667428`.

## Implementation Steps

1. Add failing unit tests for provisioning reset detection and remonitor
   `state_reset.to` detection when `new_state` does not carry the reset target.
2. Update failure epoch reset state membership to include `provisioning`.
3. Split reset conditions so state-change events use `new_state`, while
   remonitor reset events use the JSON `state_reset.to` target.
4. Re-run focused tests and lint/type checks as practical.
5. Record validation evidence in
   `plans/REVIEW_4445667428_EPOCH_EDGE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py`
  passes, if the narrower module mypy target is accepted by this repo.
