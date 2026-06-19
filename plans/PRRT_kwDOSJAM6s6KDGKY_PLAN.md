# Plan: PRRT_kwDOSJAM6s6KDGKY — Success path skips artifact deposit

## Problem statement

Review comment reports that commit `21c6a28` removed the post-validation `_deposit_planning_artifacts()` call in `src/awf/control/executor/execution_flow.py`. The reviewer argues that successful runs where planning conformance was satisfied inline (`planning_validation_handoff = None`) never copy plan/conformance files into the served artifact directory, because `_run_post_validation_conformance_check` is gated on `planning_validation_handoff is not None`. In those cases the plan artifact remains only in the worktree and is lost once the workspace is torn down.

## Requirements

1. On a successful validation run with `planning.required=True` and an inline-satisfied conformance (no post-validation handoff), the plan and conformance report must still be deposited into the served artifact directory before the worktree is removed.
2. The fix must be minimal: do not restore broad pre-validation deposits that were removed in PRRT_kwDOSJAM6s6KCdzX; only cover the missing handoff=None success path.
3. Existing terminal failure paths that already deposit through `_mark_failed_preserving_planning_artifacts` / `_enter_blocked_preserving_planning_artifacts` must keep their ordering.
4. Existing post-validation conformance handoff success path (which deposits inside `_run_post_validation_conformance_check` and then removes the worktree report) must not be redeposited redundantly.
5. The fix must be covered by a regression test that exercises the handoff=None success path and asserts the plan (and report when present) lands in the served artifact directory.
6. Pass ruff, mypy, and the targeted unit tests.

## Implementation steps

1. Inspect `execution_validation.py` around the success branch (`val_result.all_passed` and `conformance_failure is None`) to confirm the deposit gap when `planning_validation_handoff is None`.
2. Add a single best-effort deposit call in `run_validation_and_fix_cycle` on the success path, using `_planning_artifacts._deposit_planning_artifacts_best_effort(profile=profile, ...)`. Place it after the post-validation conformance check has confirmed success and before the loop breaks, but only when `planning_validation_handoff is None` (so the handoff path does not redeposit).
3. Update or add a regression test in `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py` that runs `run_validation_and_fix_cycle` with a planning-required profile, a passing validation, and `planning_validation_handoff=None`, and verifies that the plan file appears in the served artifact directory.
4. Run ruff, mypy, and targeted pytest tests.
5. Write validation document.

## Verification commands

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_validation.py
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py -q
```
