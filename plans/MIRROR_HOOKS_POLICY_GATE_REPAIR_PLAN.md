# Mirror Hooks Policy Gate Repair Plan

## Problem Statement and Scope

After executor validation succeeds, post-validation committed-output policy gates
can return before the existing mirror hooks-path repair that runs before PR push.
If a validation or post-agent command poisoned the shared mirror's
`core.hooksPath`, those early exits can leave sibling workspaces exposed.

Scope is limited to the executor's post-validation policy-gate path and focused
unit coverage for those early exits.

## Requirements Checklist

- Repair the shared mirror hooks path after successful validation and before the
  committed-output policy gates can exit.
- Keep the existing before-PR-push repair for the normal push path.
- Preserve fail-closed behavior when mirror hooks repair itself fails.
- Add focused regression coverage for the plan-only and protected committed-output
  gate paths.
- Run only targeted tests for the touched behavior; full AWF/GitHub validation is
  managed after agent completion.

## Implementation Steps

1. Add a pre-policy mirror hooks repair checkpoint in
   `src/awf/control/executor/execution_flow.py`.
2. Update existing before-PR-push repair coverage for the added successful
   checkpoint.
3. Add focused tests proving repair happens before plan-only and protected
   committed-output gate exits.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_pre_push_mirror_hooks_path.py -q`
  passes.
