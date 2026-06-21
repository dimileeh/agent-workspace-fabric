# PRRT_kwDOSJAM6s6K7p1 Fallback Head Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K7p1_FALLBACK_HEAD_PLAN.md`

## Requirement Status

- Verify the review claim against the current implementation: Complete. The old fallback paths returned the fallback SHA without calling the shared-mirror commit check.
- Validate fallback start-head SHAs against the shared mirror before returning them: Complete. Both status and candidate fallbacks now call `git --git-dir <mirror> cat-file -e <sha>^{commit}` through `_mirror_commit_object_exists`.
- Fail closed with `_REPAIR_START_HEAD_UNAVAILABLE_REASON` when a fallback SHA is dangling or the mirror cannot be resolved: Complete. Invalid or uncheckable fallback heads return the existing repair-start failure envelope.
- Preserve existing behavior for valid fallback SHAs and successful worktree `rev-parse HEAD`: Complete. Existing valid fallback tests now assert the extra mirror validation, and successful `rev-parse HEAD` still returns directly.
- Add focused regression tests for missing-worktree candidate fallback and failed-`rev-parse` status fallback: Complete. Added dangling candidate and dangling status fallback regressions.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `plans/PRRT_kwDOSJAM6s6K7p1_FALLBACK_HEAD_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K7p1_FALLBACK_HEAD_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q` passed with 26 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py` passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
