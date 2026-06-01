# PRRT_kwDOSJAM6s6GIOZP Merge Method Wording Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GIOZP_MERGE_METHOD_WORDING_PLAN.md`

## Requirement Status

- Complete: Recognize GitHub's current squash merge-method rejection wording.
- Complete: Recognize GitHub's current merge-commit merge-method rejection wording.
- Complete: Recognize GitHub's current rebase merge-method rejection wording.
- Complete: Preserve existing older wording behavior and non-method generic blocker behavior.
- Complete: Verify with focused unit tests only; AWF/GitHub own broad validation after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_pr_monitor_merge_methods.py`
- `plans/PRRT_kwDOSJAM6s6GIOZP_MERGE_METHOD_WORDING_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GIOZP_MERGE_METHOD_WORDING_VALIDATION.md`

Focused checks:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py::test_merge_method_rejection_classifier_is_specific -q`
  failed because `Merge method squash merging is not allowed` classified as `None`.
- After implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py::test_merge_method_rejection_classifier_is_specific -q`
  passed.
- After implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passed.
- After implementation, `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passed.
- After formatter adjustment, `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passed.

Broad AWF/GitHub validation was not run in the agent phase per the workspace contract.
