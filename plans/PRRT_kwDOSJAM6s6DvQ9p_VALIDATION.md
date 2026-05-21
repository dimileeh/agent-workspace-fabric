# PRRT_kwDOSJAM6s6DvQ9p Validation

Plan reference: `PRRT_kwDOSJAM6s6DvQ9p_PLAN.md`

## Requirement Status

- Invalid `repo_url` in branch open-PR resolution must not return an empty list:
  Complete. `BranchOpenPullRequestResolver.resolve` now raises
  `PullRequestMetadataError`.
- The failure must be structured so callers that already handle resolver
  exceptions classify the lookup as failed: Complete. The error uses
  `OPEN_PR_LOOKUP_INVALID`, and the worker regression verifies preserved-active
  lookup state is `failed`.
- Logs and exception details must not leak credentials embedded in repository
  URLs: Complete. The resolver reuses redacted URL/error values for both warning
  fields and exception details.
- Valid repository URLs with no open PRs must still return `[]`: Complete. The
  existing valid-url resolver test still passes.
- Add or update focused regression coverage: Complete. Updated resolver
  coverage and added a preserved-active worker regression.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client.py`
- `tests/unit/control/test_worker_coverage_edges.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestBranchOpenPullRequestResolver -q`
  initially failed before implementation because invalid repo URLs did not
  raise `PullRequestMetadataError`.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestBranchOpenPullRequestResolver tests/unit/control/test_worker_coverage_edges.py::test_preserved_active_branch_lookup_treats_invalid_repo_url_as_failure -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client.py tests/unit/control/test_worker_coverage_edges.py`
  passed.

No remaining gaps.
