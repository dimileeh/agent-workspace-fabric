# PR614 Monitor CI Recheck Plan

## Problem Statement and Scope

PR #614 reported a failing Python coverage shard with PR-monitor tests failing
around defer-signal artifacts, final base-drift rechecks, recovery log streams,
and comment-addressing monitor loops. The AWF-provided five-test focused repro
passes locally on the current branch, and the local head is newer than the
quoted failing CI run, so this fix cycle must distinguish a stale CI report from
an actual remaining order-sensitive failure before changing production code.

Scope is limited to the reported PR-monitor runtime tests and any minimal code
or test change required to make those focused failures pass. Broad validation,
full coverage gates, frontend builds, pushing, rebasing, and branch changes are
explicitly out of scope and remain AWF/GitHub-owned after agent completion.

## Requirements Checklist

- [x] Do not switch branches, push, rebase, or run broad CI-equivalent
  validation.
- [x] Run the AWF-provided focused repro command first and record the result.
- [x] Run a focused expanded subset from the reported failing pytest node IDs.
- [x] If a reproducible failure remains, identify the root cause and make the
  smallest behavior-preserving fix with focused regression coverage.
- [x] If the current branch already fixes the quoted failure, avoid unrelated
  code churn and document the evidence.
- [x] Create `plans/PR614_MONITOR_CI_RECHECK_VALIDATION.md` with requirement
  status and focused verification evidence.
- [ ] Commit any plan, validation, and code/test changes locally with a
  conventional commit message.

## Implementation Steps

1. Confirm the focused repro result from the AWF-provided command.
2. Run the remaining reported pytest node IDs as a narrow targeted group.
3. Inspect failures, if any, in the related PR-monitor runtime code and tests.
4. Add or adjust only the minimal code/tests needed for a real reproduced bug.
5. Re-run the affected focused tests.
6. Write validation notes against this plan and commit the fix-cycle artifacts.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 coverage run --parallel-mode -m pytest <AWF-provided-five-node-focused-repro> -q`
  - Pass criterion: all five reported focused repro nodes pass.
- `uv run --python 3.12 coverage run --parallel-mode -m pytest <expanded-reported-node-subset> -q`
  - Pass criterion: all targeted reported nodes pass, or any remaining failure is
    documented with a concrete blocker.
- Optional: `gh pr checks 614 --repo dimileeh/agent-workspace-fabric`
  - Pass criterion: if available, confirms whether the quoted failing run is
    stale relative to current head. This command is observational only.

Full AWF/GitHub validation is managed after this agent phase and will not be run
locally.
