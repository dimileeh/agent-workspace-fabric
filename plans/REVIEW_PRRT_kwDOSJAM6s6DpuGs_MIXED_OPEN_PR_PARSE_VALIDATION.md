# Mixed Open PR Parse Validation

Plan reference: `plans/REVIEW_PRRT_kwDOSJAM6s6DpuGs_MIXED_OPEN_PR_PARSE_PLAN.md`

## Requirement Status

- Fail closed when `gh pr list` returns both parseable PR items and malformed PR items: Complete. `list_open_pull_requests_for_branch` now raises `OPEN_PR_LOOKUP_INVALID` for mixed results.
- Preserve existing behavior for all-malformed payloads: Complete. The function still raises the first item parse failure when no item parses.
- Preserve existing behavior for fully parseable payloads: Complete. The surrounding GitHub client tests pass unchanged for successful payloads.
- Keep failure detail useful enough to diagnose malformed item positions: Complete. The mixed-result error includes parsed count, parse failure count, and failed item indexes.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `plans/REVIEW_PRRT_kwDOSJAM6s6DpuGs_MIXED_OPEN_PR_PARSE_PLAN.md`
- `plans/REVIEW_PRRT_kwDOSJAM6s6DpuGs_MIXED_OPEN_PR_PARSE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch::test_mixed_malformed_and_parseable_items_fail_closed -q`
  - Initial run failed before implementation because the code returned a partial match.
  - Rerun passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q`
  - Passed: `120 passed in 2.48s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py`
  - Passed.

## Gaps

None.
