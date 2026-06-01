# PRRT_kwDOSJAM6s6GIaFD Empty Rules Preflight Plan

## Problem Statement and Scope

The branch rules preflight treats an empty `gh api --paginate --slurp` stdout as
an API anomaly, but the raised `GitHubClientError` text does not include a
transient marker. The merge loop therefore falls through to human notification
and records `__awf_merge_method_blocked__` for the current head instead of
retrying on the next poll.

Scope is limited to:

- `src/awf/common/github_client.py`
- focused merge-method and GitHub client unit tests
- this plan and matching validation document

## Requirements Checklist

- Add a focused regression proving an empty branch-rules slurp preflight retries
  without posting a human notification or recording a merge-method blocker.
- Preserve the existing distinction between an empty JSON array (`[]`) meaning
  unconstrained branch policy and empty stdout meaning a GitHub/API anomaly.
- Make the empty slurp anomaly classify as transient through the existing
  GitHub transient preflight path.
- Run only focused local checks; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a failing merge-loop regression for the empty branch-rules slurp error.
2. Update the GitHub client empty-slurp error text to include the existing
   transient retry marker wording.
3. Strengthen the focused GitHub client test to assert the retry marker is
   present while still matching the empty-response diagnosis.
4. Run targeted pytest and ruff checks for the touched files.
5. Write validation evidence against this plan.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py::test_empty_branch_rules_slurp_preflight_error_retries_without_blocker -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "branch_pull_request_allowed_merge_methods_raises_on_empty_slurp_stdout"`
  - Passes and confirms the client preserves the empty-response diagnosis with
    retry wording.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q -k "preflight_error"`
  - Passes focused preflight coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  - Passes lint for changed code/tests.
