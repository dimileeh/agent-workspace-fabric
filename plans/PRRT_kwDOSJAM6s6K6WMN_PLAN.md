# PRRT_kwDOSJAM6s6K6WMN Plan

## Problem Statement and Scope

The review reports that `repair_mirror_hooks_path` treats an unrecognized
relative mirror `core.hooksPath`, such as `no-such-hooks`, as safe because the
classifier returns `None`. Git resolves that relative path from the repository
and bypasses the normal `.git/hooks` directory, so a missing relative hooks
directory can disable expected hooks in sibling workspaces.

Scope is limited to mirror `core.hooksPath` classification and focused unit
coverage in `tests/unit/node/test_git_manager.py`.

## Requirements Checklist

- Confirm the review against the current implementation.
- Add regression coverage for a non-allowlisted relative `core.hooksPath`.
- Repair non-allowlisted relative mirror hooks paths with the existing unset
  flow.
- Preserve the existing allowlisted legitimate relative hooks path behavior.
- Run only focused checks for the changed behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused failing test that configures `core.hooksPath` to
   `no-such-hooks` and expects `repair_mirror_hooks_path` to clear it.
2. Update `src/awf/node/git_manager.py` so the classifier allowlists only the
   known legitimate relative hooks path and rejects other relative values.
3. Run the focused mirror hooks-path test class.

## Verification Commands and Pass Criteria

Run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q
```

Pass criteria: the new regression fails before implementation when practical
and the focused `TestRepairMirrorHooksPath` selection passes after
implementation. Full AWF/GitHub validation is not run in the agent phase.
