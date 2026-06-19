# PRRT_kwDOSJAM6s6Kw21A Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Kw21A_PLAN.md`

## Requirement Status

- Complete: Add a regression proving a wildcard-ignored empty directory is dirty when `ignore_all_ignored=False`.
- Complete: Keep wildcard-ignored empty directories clean when `ignore_all_ignored=True`.
- Complete: Keep AWF agent-runtime ignored roots suppressed unconditionally.
- Complete: Keep the code change minimal and localized.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree_wildcard_ignored.py`
- `plans/PRRT_kwDOSJAM6s6Kw21A_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Kw21A_VALIDATION.md`

Focused checks:

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_wildcard_ignored.py -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree_wildcard_ignored.py`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_agent_runtime_memory_guard.py::test_empty_untracked_agent_memory_dir_is_clean -q`

No-op/mistyped check:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_agent_runtime_memory_guard.py::test_empty_agent_memory_dir_is_clean -q` collected no tests because the node id was incorrect; rerun with the exact node id above passed.

Full AWF/GitHub validation was not run inside the agent phase per workspace contract; AWF owns broad validation after completion.
