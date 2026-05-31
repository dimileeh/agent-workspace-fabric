# REVIEW_4585104324 Operator Hint Cleanup Plan

## Problem Statement And Scope

PR review feedback identified two maintainability issues in the operator-hint
monitor changes: a dead runner delegate alias for refreshing operator hints, and
an `OperatorHint.status == "agent_failed"` value that is declared and preserved
from persisted state but never written by the repair cycle.

Scope is limited to the operator-hint runner delegate surface, operator-hint
terminal-state helpers, and focused regression coverage.

## Requirements Checklist

- Remove the unused `_refresh_operator_hint_from_workspace` alias and keep
  `_refresh_operator_state_from_workspace` as the single runner delegate.
- Preserve compatibility with persisted operator hints that already contain
  terminal `agent_failed` state.
- Make a current operator-hint repair verdict of `agent_failed` persist the
  distinct `agent_failed` terminal status instead of collapsing it into
  `needs_human`.
- Add focused regression coverage for the reachable `agent_failed` operator
  hint status.
- Run only focused validation for the touched behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a failing unit test showing `_run_operator_hint_cycle()` persists
   `agent_failed` when the agent verdict is `agent_failed`.
2. Remove the unused lifecycle passthrough and its `RunnerDelegatesMixin`
   registration.
3. Add a terminal-state helper for operator-hint agent failures and route the
   `agent_failed` verdict branch to it.
4. Run the focused operator-hint test file and targeted lint for the touched
   Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/operator_hints.py src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/mixins.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
