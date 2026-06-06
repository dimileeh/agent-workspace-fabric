# Validation evidence — #431 + #432 (ws_b9e1bbf6f9d74d0da6e492de)

Iteration 2 closes the single outstanding conformance gap: no AWF-owned validation
evidence was recorded for the implementation in commit `4d5fc341b`. The implementation
itself already fully matched the saved plan (PART 1 helper adoption, PART 2a authoritative
final gate, PART 2b dead-flag removal). This file records the focused validation run.

## Scope note

Per the AWF workspace contract, the **aggregate 99% coverage gate and full-repo suite are
AWF/GitHub-CI-owned** and are not executed in the agent phase. The checks below are the
focused suites the saved plan enumerates for the touched modules, plus repo-pinned
lint/type gates (`mypy` pins `files = ["src/"]`; `ruff` scoped to touched trees).

## Results (all green)

| Check | Command (focused) | Result |
| --- | --- | --- |
| PART 1/PART 2 new tests | `pytest test_executor_part_006.py test_executor_post_agent_commit_part_004.py` | 22 passed |
| mechanical + post-agent suites | `pytest test_executor_post_agent_commit_parts coverage_edges_part_{003,007,009} runtime_profile_snapshot validation_fix_cycle[_recovery]` | 116 passed |
| error_paths / monitor_recovery / parts | `pytest test_executor_error_paths_parts test_executor_monitor_recovery_parts test_executor_parts` | 355 passed |
| lint | `ruff check src/awf/control/executor tests/unit/control` | All checks passed |
| format | `ruff format --check src/awf/control/executor` | 31 files already formatted |
| types | `mypy` (no path args; pinned to `src/`) | Success: no issues in 354 source files |

## Flag-removal confirmation

`grep -rn has_known_non_plan_output src/` → no matches (flag fully removed from production
code). The only remaining test reference is a docstring in `test_executor_part_006.py`
explaining the *old* sticky-flag behavior that the new authoritative gate replaces — not a
live reference to the deleted field.

## Coverage reasoning (gate is AWF/CI-owned)

The change is net-shrinking dead code plus rerouting two gates through existing helpers.
The new PART 1 adoption tests exercise the rerouted post-agent seam
(`_committed_and_staged_output_is_plan_only` → `_fail_if_plan_only_paths`), and the new
PART 2 tests exercise the now-unconditional final gate on both the revert false-negative
path and the normal real-output path. Removed lines (the sticky flag and its threading)
were previously covered only as pass-through; their deletion cannot lower coverage of
surviving code. The aggregate 99% gate runs in CI.
