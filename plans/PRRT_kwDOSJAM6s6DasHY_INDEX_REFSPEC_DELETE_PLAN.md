# PRRT_kwDOSJAM6s6DasHY Index Refspec Delete Plan

## Problem Statement and Scope

Address unresolved review thread `PRRT_kwDOSJAM6s6DasHY` on
`src/awf/control/protected_file_diffs.py`.

The reviewer reports that `git_show_text()` raises `RuntimeError` for missing
index-path refspecs such as `:pyproject.toml`. Staged protected-file deletion
uses that form for the new side of the diff, so a normal deletion can become an
infrastructure failure instead of an absent-content diff.

Scope is limited to the shared protected-file diff helper, focused regression
coverage, and this plan/validation documentation.

## Requirements Checklist

- Treat missing index-path refspec content such as `:pyproject.toml` as absent
  content by returning `None`.
- Preserve existing behavior for missing paths under an existing commit-ish
  base ref, returning `None`.
- Preserve existing behavior for malformed or unknown non-index refspecs,
  raising `RuntimeError` with diagnostics.
- Keep staged protected-file deletion flowing through normal
  `ProtectedFileDiff(new_text=None)` classification.
- Commit the fix locally on the current AWF-managed branch without pushing or
  switching branches.

## Implementation Steps

1. Add a focused failing unit test for `git_show_text(..., refspec=":path")`
   when `git cat-file -e` reports missing index content.
2. Add or adjust staged protected-file diff coverage to prove the new side is
   `None` for a staged deletion.
3. Update the helper so missing path recovery includes index-path refspecs with
   an empty base ref and non-empty path.
4. Run focused tests and static checks for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q -k "git_show_text"`
  fails before implementation for the new index-refspec regression and passes
  after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py -q -k "protected_file_diffs or staged_protected_file_diffs"`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k "git_show_text"`
  passes after implementation because the runtime tests import the same shared
  helper.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/protected_file_diffs.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/protected_file_diffs.py`
  passes.
