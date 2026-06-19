# PR608 Coverage Exact Threshold Validation

Plan reference: `plans/PR608_COVERAGE_EXACT_THRESHOLD_PLAN.md`

## Requirement Status

- Diagnose the CI failure from the GitHub Actions log and identify a real
  uncovered behavior in changed code: Complete.
  Evidence: `gh run view 27825438839 --repo dimileeh/agent-workspace-fabric --log-failed`
  reported combined line+branch coverage at 98.999% (78,445/79,238) below the
  99.00% threshold. The changed-code coverage report showed an uncovered branch
  in `src/awf/service/controls_guide.py` for the monitor-origin blocked guide
  path when an attempt exists without a merge candidate.

- Add or adjust a focused test that asserts behavior, not merely line execution:
  Complete.
  Evidence: `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_guide_monitor_candidate.py`
  asserts that guiding a monitor-origin blocked workspace with an attempt but no
  merge candidate resumes monitoring and records `candidate_reopened=False`
  without fabricating an open merge candidate.

- Run only targeted local validation for the changed test area: Complete.
  Evidence:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_guide_monitor_candidate.py -q`
    passed with 1 test.
  - `uv run --python 3.12 --extra dev ruff check tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_guide_monitor_candidate.py`
    passed.

- Record validation evidence in a matching validation document: Complete.
  Evidence: this document.

- Commit the fix locally with a conventional `fix(ci): ...` message: Complete.
  Evidence: local commit created for this scoped coverage fix.

## Notes

Full AWF/GitHub coverage validation was not run locally per the workspace
contract. AWF will run the broad coverage gate after agent completion.
