# REVIEW_PRRT_kwDOSJAM6s6K8u88 Pre-Push Mirror Hooks Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K8u88` reports that profile validation or post-agent commands can poison the shared mirror `core.hooksPath` after the existing post-agent repair and before the executor performs the final PR push. The scope is limited to the normal executor push path in `src/awf/control/executor/execution_flow.py` and focused regression coverage.

## Requirements Checklist

- Add a fail-closed mirror hooks-path repair immediately before the executor transitions to `pushing` and calls `push_and_open_pr`.
- Ensure failures at this point mark the workspace from the correct `validating` status.
- Preserve existing mirror repair behavior for earlier `running`-phase guards.
- Add a focused regression test proving a repair failure after validation stops before push.
- Run only targeted tests for the changed behavior; broad AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Extend the mirror repair helper with an optional failure source status that defaults to `running`.
2. Call the repair helper after pre-push policy checks and before the `validating -> pushing` transition, passing `validating`.
3. Add a focused executor regression test for the new guard.
4. Run the narrow unit test that covers the new behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_pre_push_mirror_hooks_path.py -q`
  - Passes and demonstrates the executor fails closed before PR push when mirror hook repair fails at the new guard.
