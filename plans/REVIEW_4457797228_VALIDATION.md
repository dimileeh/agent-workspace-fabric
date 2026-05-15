# REVIEW 4457797228 Validation

Plan reference: `plans/REVIEW_4457797228_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving Codex capacity recovery with no
  explicit model does not fall back to the implicit default model.
  Evidence: `tests/unit/service/test_provider_recovery.py`.
- Complete: Fixed the default Codex capacity fallback guard so
  `current_model is None` does not consume a fallback attempt to `gpt-5.5`.
  Evidence: `src/awf/service/provider_recovery.py`.
- Complete: Added a regression test proving transient CI rerun budgets stored
  under the old full failure signature, including `ci-required`, still count
  after rollup filtering.
  Evidence: `tests/unit/runtime/test_pr_monitor.py`.
- Complete: Preserved old transient rerun counts when recording a new filtered
  signature attempt.
  Evidence: `src/awf/runtime/pr_monitor.py`,
  `src/awf/runtime/pr_monitor_runner.py`, and
  `tests/unit/runtime/test_pr_monitor_runner.py`.
- Complete: Verified the protected-scope `_MonitorPolicyBlockedError` push
  result includes `MONITOR_POLICY_BLOCKED`.
  Evidence: existing `test_protected_scope_commit_repair_policy_block_uses_specific_reason`
  in `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`.
- Complete: Ran narrow behavior tests and static checks.

## Verification Evidence

- Failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_implicit_default_capacity_does_not_fallback_to_itself tests/unit/runtime/test_pr_monitor.py::TestCiFailure::test_transient_rerun_budget_reads_legacy_rollup_signature tests/unit/runtime/test_pr_monitor_runner.py::test_ci_transient_rerun_attempt_carries_legacy_rollup_count_forward -q`
  produced 3 failures.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_implicit_default_capacity_does_not_fallback_to_itself tests/unit/runtime/test_pr_monitor.py::TestCiFailure::test_transient_rerun_budget_reads_legacy_rollup_signature tests/unit/runtime/test_pr_monitor_runner.py::test_ci_transient_rerun_attempt_carries_legacy_rollup_count_forward -q`
  passed with 3 tests.
- Passed targeted behavior suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_non_default_capacity_falls_back_to_default_model tests/unit/service/test_provider_recovery.py::test_codex_default_capacity_does_not_fallback_to_itself tests/unit/service/test_provider_recovery.py::test_codex_implicit_default_capacity_does_not_fallback_to_itself tests/unit/runtime/test_pr_monitor.py::TestCiFailure tests/unit/runtime/test_pr_monitor_runner.py::test_ci_transient_rerun_attempt_treats_corrupt_count_as_zero tests/unit/runtime/test_pr_monitor_runner.py::test_ci_transient_rerun_attempt_carries_legacy_rollup_count_forward tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_policy_block_uses_specific_reason -q`
  passed with 30 tests.
- Passed static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/provider_recovery.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner.py tests/unit/service/test_provider_recovery.py tests/unit/runtime/test_pr_monitor.py tests/unit/runtime/test_pr_monitor_runner.py`
  and `uv run --python 3.12 --extra dev mypy src/awf`.
- Passed diff hygiene:
  `git diff --check`.

## Gaps

None.
