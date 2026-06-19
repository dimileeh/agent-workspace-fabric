# PR608 Current CI Coverage Fix Plan

## Problem Statement and Scope

PR #608 has reported failing CI. The current PR head is `5694ce00da828359d452b3d61fb50a37f17073e6`. GitHub Actions for that head are still running, while the latest completed failure on this PR branch was `python-full-coverage` on older head `90f4994318409354316e852c1ce4ff7200c420ba`, where combined coverage reported `98.97`, below the `99.00` threshold. The `ci-required` job failed only because `python-full-coverage` failed.

Scope is limited to fixing the current head's actionable CI failure if it reproduces. Do not edit workflow or quality-gate configuration. Do not run broad AWF/GitHub-owned validation locally.

## Requirements Checklist

- Inspect the current PR head's GitHub Actions result before changing behavior.
- If coverage fails on current head, inspect the current coverage report or logs before choosing a fix.
- Add or adjust focused behavior tests for reachable uncovered code; use a justified coverage exclusion only for genuinely unreachable, non-behavioral, or type-only code.
- Keep edits minimal and scoped to the failing behavior.
- Run focused local verification only for changed tests/code.
- Record evidence in a validation document and note that broad AWF/GitHub validation remains managed after agent completion.
- Commit the local fix with a conventional `fix(ci): ...` message and do not push.

## Implementation Steps

1. Poll the current run `27823845395` until it either fails or passes the relevant checks.
2. If a current job fails, retrieve that job's log and artifacts.
3. Identify the specific uncovered branch or line from the fresh report.
4. Write the smallest behavior test or justified exclusion that addresses the actual gap.
5. Run focused tests for the touched unit test file or module.
6. Create `plans/PR608_CURRENT_CI_COVERAGE_FIX_VALIDATION.md`.
7. Commit the resulting plan, validation, and code/test changes locally.

## Verification Commands and Pass Criteria

- `gh run view 27823845395 --json conclusion,status,jobs`
  - Pass criterion: current run status and any failed job are identified.
- Focused pytest command for any changed test file.
  - Pass criterion: selected tests pass locally.
- Optional focused lint on changed Python files only if Python source is edited.
  - Pass criterion: no lint errors in touched files.

Full CI, full coverage, and broad repository validation are intentionally left to AWF/GitHub after this agent finishes.
