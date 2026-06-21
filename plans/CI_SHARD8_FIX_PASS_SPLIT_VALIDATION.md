# CI shard 8 fix-pass split validation

Plan reference: `plans/CI_SHARD8_FIX_PASS_SPLIT_PLAN.md`

## Requirement status

- Complete: Preserved AWF branch ownership. No branch switch, push, rebase, or
  broad AWF/GitHub validation was run.
- Complete: Reproduced the shard-8 line-limit failure before editing.
- Complete: Split the oversized fix-pass test module at natural test boundaries.
- Complete: Kept touched first-party files under the 1500-line limit.
- Complete: Ran focused verification for the split tests and line-limit guardrail.
- Complete: Full AWF/GitHub validation remains delegated to AWF after this agent
  phase.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Before: failed with part 002 at 1625 lines.
  - After: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py -q`
  - Result: `29 passed`.
- `uv run --python 3.12 --extra dev ruff check ...part_002.py ...part_006.py`
  - Result: passed.

## Residual risk

Full coverage and the complete CI matrix were intentionally not run locally per
the AWF workspace contract.
