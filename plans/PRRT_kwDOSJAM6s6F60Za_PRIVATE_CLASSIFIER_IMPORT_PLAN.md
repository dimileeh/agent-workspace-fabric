# PRRT_kwDOSJAM6s6F60Za Private Classifier Import Plan

## Problem Statement and Scope

The PR monitor pre-commit autofix helper imports
`_classify_post_agent_commit_failure` from
`awf.control.executor.quality_gates`. That symbol is private and lives in the
executor quality-gate layer, so importing it from the monitor runner creates a
fragile cross-layer dependency.

Scope is limited to removing the monitor runner's private executor import while
preserving current monitor autofix behavior.

## Requirements Checklist

- Add regression coverage that fails while `commit_autofix.py` imports executor
  quality-gate internals.
- Preserve monitor autofix behavior for deterministic hooks and semantic hook
  rejection.
- Avoid editing protected executor quality-gate files.
- Run focused validation only; AWF/GitHub own broad validation after this agent
  phase.

## Implementation Steps

1. Add a focused regression test for the forbidden import boundary.
2. Move the monitor's needed pre-commit output parsing into a monitor-owned
   helper module.
3. Update `commit_autofix.py` to use the monitor-owned helper.
4. Run the targeted runtime monitor autofix unit tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  passes.
- Full AWF/GitHub validation is not run locally per the workspace contract.
