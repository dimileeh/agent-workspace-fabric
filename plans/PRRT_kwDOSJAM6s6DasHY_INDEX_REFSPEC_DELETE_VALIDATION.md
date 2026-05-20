# PRRT_kwDOSJAM6s6DasHY Index Refspec Delete Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DasHY_INDEX_REFSPEC_DELETE_PLAN.md`

## Requirement Status

- Complete: Missing index-path refspec content such as `:pyproject.toml` now
  returns `None` from `git_show_text()`.
- Complete: Missing paths under existing commit-ish base refs still return
  `None`; the existing regression remains covered.
- Complete: Unknown non-index refspecs still raise `RuntimeError` when their
  base ref cannot be verified.
- Complete: Staged protected-file deletion now produces
  `ProtectedFileDiff(new_text=None)` through `_protected_file_diffs_for_staged_paths()`.
- Complete: The fix is local to the current AWF-managed branch; no branch
  switch or push was performed.

## Evidence

Changed files:

- `src/awf/control/protected_file_diffs.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/PRRT_kwDOSJAM6s6DasHY_INDEX_REFSPEC_DELETE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DasHY_INDEX_REFSPEC_DELETE_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q -k "git_show_text"`
  - Result before implementation: failed for
    `test_git_show_text_returns_none_for_missing_index_path`.
  - Result after implementation: passed, 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py -q -k "protected_file_diffs or staged_protected_file_diffs"`
  - Result after implementation: passed, 17 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q`
  - Result: passed, 15 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q -k "staged_protected_file_diffs"`
  - Result: passed, 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k "git_show_text"`
  - Result: passed, 4 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/protected_file_diffs.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/protected_file_diffs.py`
  - Result: passed.

No gaps remain.
