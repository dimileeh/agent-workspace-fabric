# PRRT_kwDOSJAM6s6DdvJK Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DdvJK_PLAN.md`

## Requirement Status

- Request `headRepository` / `headRepositoryOwner` from `gh pr list`: Complete.
  `_BRANCH_OPEN_PR_LIST_JSON_FIELDS` now includes both fields.
- Parse and carry PR head repository identity in branch lookup results: Complete.
  `BranchOpenPullRequest` now carries `head_repo_slug`, parsed from
  `headRepository.nameWithOwner` or `headRepositoryOwner.login` plus
  `headRepository.name`.
- Reject or mark operator-required when a single branch match is from a
  different head repository than the workspace repository: Complete. The
  preserved active branch lookup now returns an ambiguous
  `open_pr_head_repo_mismatch` result, which the existing salvage flow records
  as operator-required instead of attaching the monitor.
- Preserve valid same-repository single-match salvage behavior: Complete.
  Existing pushed-branch PR salvage tests still pass.
- Add regression tests for parser field coverage and mismatched-fork ambiguity:
  Complete. Added focused tests in `tests/unit/common/test_github_client.py` and
  `tests/unit/control/test_worker.py`.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `src/awf/control/worker.py`
- `tests/unit/common/test_github_client.py`
- `tests/unit/control/test_worker.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::TestListOpenPullRequestsForBranch -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "pushed_branch_pr" -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/control/worker.py tests/unit/common/test_github_client.py tests/unit/control/test_worker.py`
- `uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py src/awf/control/worker.py`

All listed commands passed.

## Gaps

None.
