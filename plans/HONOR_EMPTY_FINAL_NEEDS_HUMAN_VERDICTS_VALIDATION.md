# Honor Empty Final Needs Human Verdicts Validation

Plan reference: `plans/HONOR_EMPTY_FINAL_NEEDS_HUMAN_VERDICTS_PLAN.md`

## Requirement Status

- Complete: Preserve final AWF-prefixed `NEEDS_HUMAN` when its reason is empty or sanitized away.
- Complete: Preserve existing safety behavior for sanitized final non-blocking AWF verdicts that would otherwise override a prior blocking verdict.
- Complete: Add focused regression coverage for the review-thread scenario.
- Complete: Run only targeted tests for the touched parser behavior.
- Complete: Do not run broad AWF or CI-equivalent validation; AWF/GitHub owns broad validation after agent completion.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/helpers.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py`
- `plans/HONOR_EMPTY_FINAL_NEEDS_HUMAN_VERDICTS_PLAN.md`
- `plans/HONOR_EMPTY_FINAL_NEEDS_HUMAN_VERDICTS_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py -q`
  - Result before implementation: failed on sanitized final `FIXED` clearing a prior `NEEDS_HUMAN`.
  - Result after implementation: `32 passed`.
- `uv run --python 3.12 --extra dev ruff format tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py`
  - Result: reformatted the touched test file.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py`
  - Result: passed.

Full AWF/GitHub validation was not run inside the agent phase, per the workspace contract.
