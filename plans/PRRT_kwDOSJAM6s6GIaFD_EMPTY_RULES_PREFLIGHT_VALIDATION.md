# PRRT_kwDOSJAM6s6GIaFD Empty Rules Preflight Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GIaFD_EMPTY_RULES_PREFLIGHT_PLAN.md`

## Requirement Status

- Add a focused regression proving an empty branch-rules slurp preflight retries
  without posting a human notification or recording a merge-method blocker:
  Complete. Added
  `test_empty_branch_rules_slurp_preflight_error_retries_without_blocker`.
- Preserve the existing distinction between `[]` as unconstrained branch policy
  and empty stdout as a GitHub/API anomaly: Complete. The existing empty-array
  behavior remains unchanged; empty stdout still raises `GitHubClientError`.
- Make the empty slurp anomaly classify as transient through the existing
  GitHub transient preflight path: Complete. The error text now includes the
  established `try again` transient marker.
- Run only focused local checks: Complete. Full AWF/GitHub validation was not
  run and remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/runtime/test_pr_monitor_merge_methods.py`
- `tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
- `plans/PRRT_kwDOSJAM6s6GIaFD_EMPTY_RULES_PREFLIGHT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GIaFD_EMPTY_RULES_PREFLIGHT_VALIDATION.md`

Focused checks:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py::test_empty_branch_rules_slurp_preflight_error_retries_without_blocker -q`
  failed because the monitor posted a human notification for the empty branch
  rules slurp error.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py::test_empty_branch_rules_slurp_preflight_error_retries_without_blocker -q`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "branch_pull_request_allowed_merge_methods_raises_on_empty_slurp_stdout"`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q -k "preflight_error"`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/common/github_client.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  passed.

## Gaps

No gaps. Broad validation and merge-gate provenance are intentionally left to
AWF/GitHub after agent completion per the workspace contract.
