# PRRT_K8YTX Included HooksPath Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6K8yTx` reports that
`repair_mirror_hooks_path()` probes mirror and worktree config without
respecting Git `include.path` directives. An included config can set
`core.hooksPath`, while the current repair returns clean because the probe does
not pass `--includes`.

## Scope

- Limit changes to mirror/worktree hook-path repair behavior.
- Add focused regression coverage for an included config that contributes
  `core.hooksPath`.
- Do not run broad AWF/GitHub validation; AWF owns that after agent completion.

## Requirements Checklist

- Add a regression test showing an included `core.hooksPath` is detected and no
  longer trusted as clean.
- Update the repair path to inspect included config values.
- Ensure the repair leaves Git lookup clean, either by removing the included
  hook-path value or the include that exposes it.
- Preserve existing direct `core.hooksPath` repair behavior.

## Implementation Steps

1. Reproduce Git config include behavior with a focused test case.
2. Update `_repair_hooks_path_config()` to include Git include directives during
   hook-path probes and reprobes.
3. If included values cannot be unset directly through the including config,
   remove the matching include path from the inspected config file so the
   poisoned hook path is no longer visible.
4. Run the focused node unit test(s) covering mirror hook repair.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
- Record that full AWF/GitHub validation is intentionally left to AWF.
