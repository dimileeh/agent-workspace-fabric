# PRRT_kwDOSJAM6s6D2X2X Single-Read Ownership Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6D2X2X_SINGLE_READ_OWNERSHIP_PLAN.md`

## Requirement Status

- Complete: Added a regression test for mirror/gitdir divergence during runtime
  ownership repair.
- Complete: Runtime ownership validation now resolves linked gitdir metadata
  once.
- Complete: Mirror derivation now uses the same linked gitdir value that is
  validated and passed to repair.
- Complete: Missing or unresolvable runtime layout now fails closed through the
  existing logged repair-failure path.
- Complete: Existing valid linked-worktree, symlinked mirror, and numeric suffix
  behavior remains covered by the runtime ownership test suite.

## Evidence

Files changed:

- `src/awf/runtime/ownership.py`
- `tests/unit/runtime/test_ownership.py`
- `plans/PRRT_kwDOSJAM6s6D2X2X_SINGLE_READ_OWNERSHIP_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6D2X2X_SINGLE_READ_OWNERSHIP_VALIDATION.md`

Commands run:

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py::test_repair_agent_runtime_ownership_blocks_divergent_git_metadata_reads -q`
  failed because the existing implementation returned success after divergent
  `.git` reads.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py::test_repair_agent_runtime_ownership_blocks_divergent_git_metadata_reads -q`
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
- Passing after implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`
- Passing after implementation:
  `uv run --python 3.12 --extra dev mypy src/awf`

## Gaps

None.
