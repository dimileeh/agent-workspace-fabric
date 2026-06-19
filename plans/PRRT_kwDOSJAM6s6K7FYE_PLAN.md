# PRRT_kwDOSJAM6s6K7FYE Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6K7FYE` reports that executor mirror hooks-path
repair runs before profile setup, but a failing setup/pre-agent command can
poison the shared mirror config and then return through the setup failure branch
before the later pre-agent-launch repair.

Scope is limited to `src/awf/control/executor/execution_flow.py` and focused
unit coverage in `tests/unit/control/test_executor_mirror_hooks_path.py`.

## Requirements

- Add a focused regression proving a setup failure triggers a second mirror
  hooks-path repair before returning.
- Preserve the existing setup failure status when the post-setup repair
  succeeds.
- Fail closed with the mirror repair reason if the post-setup repair itself
  fails.
- Do not run broad AWF/GitHub validation; use targeted tests only.

## Implementation Steps

1. Add a failing unit test for setup failure after an initially successful
   pre-setup mirror repair.
2. Update the setup failure branch to rerun mirror hooks-path repair before
   marking the workspace failed for setup.
3. Run the focused mirror hooks-path test module.
4. Record validation evidence in the matching validation document.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Pass criteria: the new regression and existing focused mirror hook-path
    executor tests pass.
