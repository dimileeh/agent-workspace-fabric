# PRRT_kwDOSJAM6s6K5eEw Plan

## Problem Statement and Scope

The mirror hooks-path repair helper removes only two exact poisoned
`core.hooksPath` values. The review reports that an attacker-chosen absolute
empty hook directory, such as `/tmp/empty-hooks`, is not repaired and can cause
future shared-mirror commits to bypass the mirror's expected hooks.

Scope is limited to `repair_mirror_hooks_path` classification and focused unit
coverage in `tests/unit/node/test_git_manager.py`.

## Requirements Checklist

- Add a regression test for an unrecognized absolute mirror `core.hooksPath`.
- Repair unrecognized absolute mirror hooks paths with the existing unset flow.
- Preserve the existing behavior for legitimate relative project hook paths.
- Keep duplicate/concurrent cleanup behavior and reason-code failures intact.

## Implementation Steps

1. Add a focused failing test that configures `/tmp/empty-hooks` and expects the
   helper to remove it.
2. Update `src/awf/node/git_manager.py` to classify unrecognized absolute
   hooks paths as disallowed while keeping relative project hook paths allowed.
3. Run the focused mirror hooks-path test class.

## Verification Commands and Pass Criteria

Run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q
```

Pass criteria: all focused `TestRepairMirrorHooksPath` tests pass. Full
AWF/GitHub validation remains owned by AWF after agent completion.
