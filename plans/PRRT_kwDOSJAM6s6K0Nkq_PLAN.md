# PRRT_kwDOSJAM6s6K0Nkq Plan

## Problem Statement and Scope

The mirror hooks path repair currently clears a poisoned bare mirror by running
`git config --unset core.hooksPath`. Git fails this command when duplicate
`core.hooksPath` values exist, leaving the poisoned mirror unrepaired.

Scope is limited to clearing all mirror `core.hooksPath` entries and adding a
focused regression test.

## Requirements Checklist

- Add a regression test for a bare mirror with duplicate `core.hooksPath`
  entries.
- Make `repair_mirror_hooks_path` remove all `core.hooksPath` entries when
  repair is needed.
- Preserve existing repair failure behavior and reason codes.
- Run focused tests only; broad AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/node/test_git_manager.py` that
   creates duplicate `core.hooksPath` entries with `git config --add`.
2. Change the repair command from `--unset` to `--unset-all`.
3. Run the focused `TestRepairMirrorHooksPath` tests.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6K0Nkq_VALIDATION.md`.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q
```

Pass criteria: all focused mirror hooks path repair tests pass.
