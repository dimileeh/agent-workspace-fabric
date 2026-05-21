# PRRT_kwDOSJAM6s6D2X2X Single-Read Ownership Plan

## Problem Statement And Scope

The PR review thread reports that runtime ownership repair validates mirror
layout with one `.git` read and then passes a separately read linked gitdir to
the repair helper. If the two reads diverge, the helper can receive an
unvalidated linked gitdir. This change is scoped to `awf.runtime.ownership`
and its unit tests.

## Requirements Checklist

- Add a regression test for mirror/gitdir divergence during runtime ownership
  repair.
- Resolve linked gitdir metadata once inside the runtime ownership validator.
- Derive the mirror path from the same linked gitdir value used for validation
  and repair.
- Fail closed when runtime ownership repair cannot resolve a validated mirror.
- Preserve existing valid linked-worktree, symlinked mirror, and numeric suffix
  behavior.

## Implementation Steps

1. Add a failing unit test in `tests/unit/runtime/test_ownership.py` that
   simulates divergent mirror discovery and linked gitdir reads.
2. Replace the separate `mirror_path_for_worktree` call in
   `src/awf/runtime/ownership.py` with mirror derivation from the single linked
   gitdir read.
3. Keep repair calls passing both the validated mirror and validated linked
   gitdir so lower-level fallback discovery is not used by this runtime path.
4. Run focused runtime ownership tests, then lint/typecheck the touched area as
   practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes or any unrelated pre-existing failure is documented.
