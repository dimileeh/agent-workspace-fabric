# REVIEW 4457797228 History-Rewritten Validation

Plan reference:
`plans/REVIEW_4457797228_HISTORY_REWRITTEN_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for both clean no-commit outcomes.
  Evidence: `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py` now
  parametrizes the protected-scope committed-repair log test so changed `HEAD`
  requires `history_rewritten=True` and unchanged `HEAD` requires
  `history_rewritten=False`.
- Complete: Preserved the existing clean repair and push behavior.
  Evidence: the same regression still returns a successful `_GitPushResult` in
  both cases.
- Complete: Added the runtime audit/log signal requested by the review.
  Evidence: `src/awf/runtime/pr_monitor_runner.py` samples `HEAD` before and
  after the monitor-agent repair run and includes `history_rewritten` on
  `monitor.protected_scope_committed_repair_commit_not_created`.
- Complete: Verified the provider recovery default-model concern remains
  handled as stale feedback.
  Evidence: `tests/unit/service/test_provider_recovery.py::test_codex_capacity_without_effective_default_skips_implicit_fallback`
  passed, proving no compiled-in default fallback is selected without an
  effective default model.
- Complete: Ran focused tests, lint, and diff hygiene.

## Verification Evidence

- Failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created -q`
  failed with `KeyError: 'history_rewritten'`.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created -q`
  passed with 2 tests.
- Passed provider recovery stale-review coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_capacity_without_effective_default_skips_implicit_fallback -q`
  passed with 1 test.
- Passed lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/service/test_provider_recovery.py`.
- Passed diff hygiene:
  `git diff --check`.

## Gaps

None.
