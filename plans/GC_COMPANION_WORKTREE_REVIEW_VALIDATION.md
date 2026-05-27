# GC Companion Worktree Review Validation

Plan reference: `plans/GC_COMPANION_WORKTREE_REVIEW_PLAN.md`

## Requirement Status

- Add regression coverage for partial worktree-removal error attribution:
  Complete. `test_gc_partial_worktree_remove_deletes_successful_worktree_paths`
  now asserts that `delete_errors` points at the failed companion worktree path
  and preserves the target-specific error.
- Add regression coverage for malformed companion policy entries with `name` but
  no `repo_url`: Complete.
  `test_gc_companion_worktree_paths_ignore_name_only_policy_entries` covers the
  aligned candidate/target invariant.
- Preserve existing per-target deletion decisions and path outcomes: Complete.
  Existing assertions still prove successful worktree paths are deleted while
  failed companion paths are skipped.
- Keep the change scoped to GC companion worktree handling: Complete. Code
  changes are limited to `src/awf/service/gc.py` and
  `src/awf/service/gc_companions.py`.
- Do not run broad AWF/GitHub-owned validation: Complete. Only focused local
  checks were run; full AWF/GitHub validation remains managed by AWF after agent
  completion.

## Evidence

- Updated `src/awf/service/gc.py` to derive worktree-removal delete diagnostics
  from failed per-target results when available.
- Updated `src/awf/service/gc_companions.py` so companion GC path discovery and
  remove-target discovery share the same required fields.
- Updated `tests/unit/service/test_gc_more2.py` with focused regressions.

## Commands Run

- Initial failing check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py::test_gc_partial_worktree_remove_deletes_successful_worktree_paths tests/unit/service/test_gc_more2.py::test_gc_companion_worktree_paths_ignore_name_only_policy_entries -q`
  failed before implementation with the expected attribution and malformed
  companion candidate failures.
- Final focused checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py::test_gc_partial_worktree_remove_deletes_successful_worktree_paths tests/unit/service/test_gc_more2.py::test_gc_companion_worktree_paths_ignore_name_only_policy_entries -q`
  passed.
- Formatter:
  `uv run --python 3.12 --extra dev ruff format tests/unit/service/test_gc_more2.py`
  reformatted the touched test file.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -q`
  passed: 36 tests before and after formatting.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/service/gc_companions.py tests/unit/service/test_gc_more2.py`
  passed.

## Gaps

None.
