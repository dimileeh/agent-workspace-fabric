# PRRT_kwDOSJAM6s6FHsmu Optional Validation Plan

## Problem Statement and Scope

The review thread reports that `build_release_pr_monitor` and
`build_feature_pr_monitor` now require a `validation` argument even though
`PullRequestMonitorRunner` still accepts `validation=None`. This can break
existing builder callers that rely on the runner's optional validation
contract.

Scope is limited to the PR monitor factory signatures, focused regression
coverage, and this plan/validation record.

## Requirements Checklist

- Preserve existing builder behavior when a `ValidationRunner` is supplied.
- Allow both builder functions to be called without `validation`.
- Pass `validation=None` through to `PullRequestMonitorRunner` when omitted.
- Add a regression test for omitted validation on both builder functions.
- Run focused validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add failing regression coverage in `tests/unit/runtime/test_release_pr_monitor.py`
   for omitted `validation`.
2. Update `src/awf/runtime/release_pr_monitor.py` so both builders default
   `validation` to `None`.
3. Run the focused unit test file and targeted static checks for changed files.
4. Record results in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_release_pr_monitor.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/release_pr_monitor.py tests/unit/runtime/test_release_pr_monitor.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/release_pr_monitor.py`
  passes.
