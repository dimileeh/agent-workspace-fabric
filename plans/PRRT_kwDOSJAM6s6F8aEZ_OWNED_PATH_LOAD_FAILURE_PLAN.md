# PRRT_kwDOSJAM6s6F8aEZ Owned Path Load Failure Plan

## Problem Statement

The review thread reports that PR-monitor repair prompts silently fall back to an
empty `owned_paths` list when the workspace owned-path lookup fails. That can
make CI repair or direct review-comment repair prompts treat owned protected
files as unowned protected blockers after a transient session or database
failure.

## Scope

- Keep the change inside PR-monitor repair prompt construction and focused unit
  tests.
- Do not change protected workflow/configuration files.
- Do not run broad AWF/GitHub validation; use targeted unit tests only.

## Requirements

- [ ] Owned-path lookup failures must not produce repair prompts with an empty
      owned-path list.
- [ ] Direct thread/review-comment repair entry points must fail before invoking
      the repair agent if owned paths cannot be loaded.
- [ ] CI repair prompt construction must fail before invoking the repair agent
      if owned paths cannot be loaded.
- [ ] Existing explicit-empty-owned-path behavior for real empty ownership must
      remain available through the repository lookup returning `[]`.

## Implementation Steps

1. Replace fallback-owned-path helper usage with the existing strict
   `_owned_paths_for_prompt` loader in direct comment repair and CI repair.
2. Remove the broad exception-catching fallback helper if it has no remaining
   call sites.
3. Update/add focused unit tests for direct thread/review repair and CI repair
   failures.
4. Run targeted unit tests covering the edited behavior.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`

Full AWF/GitHub validation remains owned by AWF after agent completion.
