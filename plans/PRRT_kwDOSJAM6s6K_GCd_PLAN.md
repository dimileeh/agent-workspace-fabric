# PRRT_kwDOSJAM6s6K-GCd validation stop mirror repair plan

## Problem and Scope

The executor repairs poisoned shared mirror `core.hooksPath` before PR push, but
the validation phase can return `stop=True` before that repair runs. If a
validation command or fix pass poisons the mirror and then fails terminally, later
workspaces can inherit the poisoned mirror.

Scope is limited to the executor validation stop return path and focused
regression coverage.

## Requirements

- Add a regression proving the executor attempts mirror hooks-path repair when
  `run_validation_and_fix_cycle` returns `stop=True`.
- Fail closed with the existing `MIRROR_HOOKS_PATH_REPAIR_FAILED` handling if
  that stop-path repair fails.
- Preserve existing successful validation and PR-push repair behavior.
- Do not run broad AWF/GitHub-owned validation; use focused tests only.

## Implementation Steps

1. Add a focused unit test in the existing executor mirror-hooks regression file.
2. Run that new test and confirm it fails on the current code.
3. Add the missing repair call before returning on `validation_result.stop`.
4. Re-run the focused test and a narrow mirror-hooks test subset.

## Verification

- New regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q -k validation_stop`
- Narrow adjacent coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py tests/unit/control/test_executor_pre_push_mirror_hooks_path.py -q -k "validation_stop or validation_before_pr_push"`

Full AWF/GitHub validation remains owned by AWF after agent completion.
