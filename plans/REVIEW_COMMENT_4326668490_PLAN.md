# Review Comment 4326668490 Plan

## Problem Statement And Scope

PR review comment `4326668490` summarizes several actionable and nitpick findings
against PR #272. Verify each finding against the current checkout, fix only
still-valid issues, and keep existing safety and regression behavior intact.

## Requirements Checklist

- Verify the `github_client.py` open-PR item URL parsing concern.
- If invalid open-PR repo URLs are silently ignored, add a warning while keeping
  the `[]` fallback behavior.
- Verify the worker-restart executor recovery claim exclusivity concern.
- Verify the preserved-active git subprocess timeout concern.
- Verify the active-execution salvage duplicate-event race concern.
- Isolate `BranchOpenPullRequestResolver` in the affected service worker runtime
  unit test.
- Add or update regression coverage for behavior changes.
- Do not push, switch branches, weaken existing assertions, or edit unrelated
  files.

## Implementation Steps

1. Inspect current code and tests for each finding.
2. Mark already-addressed findings as no-op in validation evidence.
3. Add a focused regression test for invalid repo URL warning behavior.
4. Add the missing service runtime monkeypatch for
   `BranchOpenPullRequestResolver`.
5. Implement the minimal warning in `BranchOpenPullRequestResolver.resolve`.
6. Run focused unit tests for the changed areas.
7. Write validation evidence and commit the local fix.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py tests/unit/service/test_worker.py -q`
  passes.
- `git status --short` shows only intentional files before staging.
