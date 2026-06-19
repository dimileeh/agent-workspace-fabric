# PRRT_kwDOSJAM6s6KxJti Plan

## Problem Statement and Scope

The review thread reports that `repair_mirror_hooks_path()` treats every
non-zero `git config --local core.hooksPath` probe exit as "not configured",
which can mask real mirror/config probe failures. Scope is limited to the probe
handling in `src/awf/node/git_manager.py` and focused unit coverage in
`tests/unit/node/test_git_manager.py`.

## Requirements Checklist

- Preserve the existing no-op result when `core.hooksPath` is genuinely unset.
- Preserve the existing repair behavior when `core.hooksPath` is set.
- Raise `GitOperationError` when the probe fails for a reason other than an
  unset config key, including captured stdout/stderr and the existing mirror
  hooks-path failure reason code.
- Keep changes minimal and avoid unrelated refactors.

## Implementation Steps

1. Add a focused regression test showing a probe failure raises
   `GitOperationError`.
2. Update `repair_mirror_hooks_path()` so probe return code `1` remains the
   unset/no-op case, while other non-zero exits raise.
3. Run targeted tests for `TestRepairMirrorHooksPath` only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q`
  must pass.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
