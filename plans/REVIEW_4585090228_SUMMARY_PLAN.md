# Review 4585090228 Summary Plan

## Problem Statement And Scope

The review-level Greptile summary for PR #328 called out two remaining P2
edge-case observations around dispatch-time host-port conflict handling:

- Auto-resolved profile service ports are checked after companion ports in a
  separate transaction because those profile ports are unknown until provisioner
  profile resolution.
- `cancel_workspace(stop_stack=False)` can transiently leave a terminal
  workspace with `compose_project_name` set and no
  `workspace.terminal_runtime_released` event if it wins after the provisioner
  pre-launch stamp but before Docker starts.

The current behavior is intentionally conservative. Scope is limited to making
those boundaries explicit in code comments so future changes do not accidentally
weaken the host-port safety model.

## Requirements Checklist

- [ ] Document that auto-profile host-port admission has a known boundary before
  `_check_auto_resolved_profile_host_ports` publishes `resolved_profile`; a
  concurrent first committer can win and cause the current workspace to fail
  before launch.
- [ ] Document why `cancel_workspace(stop_stack=False)` does not emit
  `workspace.terminal_runtime_released` and why the resulting pre-launch
  false-positive port block is bounded by cleanup.
- [ ] Keep the change comment-only with no behavioral regression surface.
- [ ] Run focused lint for touched Python files; full AWF/GitHub validation
  remains owned by AWF after agent completion.
- [ ] Commit the focused fix locally without pushing.

## Implementation Steps

1. Extend the `_check_auto_resolved_profile_host_ports` docstring with the
   companion/profile transaction boundary and first-committer-wins outcome.
2. Add an explanatory comment in `cancel_workspace` before the terminal runtime
   release event guard.
3. Run focused `ruff check` on the touched Python files.
4. Record validation evidence in
   `plans/REVIEW_4585090228_SUMMARY_VALIDATION.md`.
5. Stage only the files changed for this review-level comment and commit with
   the requested conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py src/awf/service/controls.py`
  - Passes with no lint failures.

Full repository tests, coverage gates, frontend builds, and CI-equivalent
validation are intentionally not run in the agent phase per the AWF workspace
contract.
