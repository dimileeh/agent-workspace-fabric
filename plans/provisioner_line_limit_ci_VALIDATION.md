# Provisioner Line Limit CI Validation

Plan reference: `plans/provisioner_line_limit_ci_PLAN.md`

## Requirement Status

- Reproduce the focused CI failure before editing: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
    failed before edits with `src/awf/node/provisioner.py: 1599`.
- Keep `src/awf/node/provisioner.py` below the 1,500-line limit: Complete.
  - Evidence: `wc -l src/awf/node/provisioner.py src/awf/node/provisioner_helpers.py`
    reports `1429 src/awf/node/provisioner.py` and `203 src/awf/node/provisioner_helpers.py`.
- Preserve existing imports of helper names from `awf.node.provisioner`: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q`
    passed with 39 tests.
- Do not weaken, skip, or modify the maintainability check: Complete.
  - Evidence: no edits to `tests/unit/test_core_decomposition_maintainability.py`; the focused guardrail passed unchanged.
- Run focused verification only: Complete.
  - Evidence: focused pytest, targeted ruff, and targeted mypy commands were run; full AWF/GitHub validation and coverage gates were not run locally per workspace contract.
- Commit the fix locally with a conventional commit message: Complete.
  - Evidence: this validation file is included in the local fix commit created
    at completion.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: `1 passed in 0.41s`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q`
  - Result: `39 passed in 18.37s`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py src/awf/node/provisioner_helpers.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/node/provisioner.py src/awf/node/provisioner_helpers.py`
  - Result: `Success: no issues found in 2 source files`

Full AWF/GitHub validation is intentionally left to the AWF post-agent and CI
merge-gating flow.
