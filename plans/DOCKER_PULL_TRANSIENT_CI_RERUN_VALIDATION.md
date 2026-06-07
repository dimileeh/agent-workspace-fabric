# Validation — Docker pull / service-container registry timeout transient CI

Implements `plans/DOCKER_PULL_TRANSIENT_CI_RERUN_PLAN.md`.

## Change summary

- `src/awf/runtime/pr_monitor.py`: added three transport-timeout markers to
  `_CI_TRANSIENT_FAILURE_MARKERS` — `"context deadline exceeded"`,
  `"client.timeout exceeded while awaiting headers"`,
  `"request canceled while waiting for connection"`. No other logic changed; the
  structured/code-failure short-circuit in `_looks_like_transient_ci_failure`
  still runs first, and `_should_rerun_transient_ci`/`decide` gates are untouched.
- `tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py`: added
  three regression tests (TDD — written before the marker addition):
  - `test_docker_pull_registry_timeout_dispatches_rerun` (full PR #449 log → `RerunTransientCI`)
  - `test_docker_pull_request_canceled_dispatches_rerun` (second signature → `RerunTransientCI`)
  - `test_docker_pull_with_structured_test_evidence_reports_ci_failure`
    (timeout log + structured test evidence → `ReportCiFailure`, safeguard)

## Commands run (focused)

```
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py -q \
  -k "transient or docker_pull"
=> 19 passed, 80 deselected

uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/pr_monitor.py \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py
=> All checks passed!

uv run --python 3.12 --extra dev ruff format --check \
  src/awf/runtime/pr_monitor.py \
  tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py
=> 2 files already formatted

uv run --python 3.12 --extra dev mypy
=> Success: no issues found in 354 source files
```

## Coverage reasoning

The new markers are exercised by the two `RerunTransientCI` tests; the unchanged
structured-evidence safeguard branch is exercised by the `ReportCiFailure` test.
No new uncovered lines or branches are introduced. Full coverage gate
(`pytest --cov`) and CI-equivalent suites are owned by AWF/GitHub after the agent
finishes and were not run locally per the AWF workspace contract.

## Safeguards preserved

- Structured test/code-failure evidence (`test_node_ids`/`assertion_snippets`)
  still forces `ReportCiFailure` even when a transport-timeout marker is present
  (locked in by the third test).
- No generic `docker pull` / `exit code 1` marker added, so ordinary Docker
  build/test failures without a transport-timeout phrase remain non-transient.
