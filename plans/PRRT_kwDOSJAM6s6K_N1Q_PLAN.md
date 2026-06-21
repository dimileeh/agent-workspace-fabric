# PRRT_kwDOSJAM6s6K_N1Q Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6K_N1Q` reports that concurrent
`repair_mirror_hooks_path()` calls can race while removing the same included
config. One repair can remove the include after another repair has already
probed stale `core.hooksPath` data; the second repair then sees
`_unset_matching_include_path()` return `False` and raises
`MIRROR_HOOKS_PATH_REPAIR_FAILED` even though the mirror has already been
repaired.

Scope is limited to the stale included-config branch in
`src/awf/node/git_manager.py` and focused regression coverage in the existing
mirror hook repair tests.

## Requirements Checklist

- Add a focused regression for a stale included config removed between probe
  and unset.
- Accept the already-repaired case only after re-probing confirms that the
  stale included origin no longer exposes a disallowed `core.hooksPath`.
- Preserve the existing failure behavior when the included origin still exposes
  a poisoned or disallowed hooks path.
- Keep validation narrow; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add a unit test that simulates another repair removing the include before
   `_unset_matching_include_path()` can remove it.
2. Run the new test and confirm it fails before implementation when practical.
3. Update `_repair_hooks_path_config()` to re-probe on a failed include unset
   and continue only when the included origin is already absent from the current
   disallowed hooks path set.
4. Run the focused unit test(s) covering mirror hook repair.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_tolerates_concurrent_include_repair -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  should pass as the focused affected surface.
- Do not run full repository tests, coverage gates, frontend builds, or other
  AWF/GitHub-owned broad validation in this agent phase.
