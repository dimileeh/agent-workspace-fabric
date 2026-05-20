# Review 4491715538 Multiline Workflow Step Line Plan

## Problem Statement And Scope

The review comment reports four quality-gate classifier concerns. Existing
plan, docs, and regression tests already preserve the intentionally narrow
GitHub Actions expression allowlist and the policy that unowned coverage policy
edits remain blocked, so this slice will not weaken those safety checks.

The actionable gap is diagnostic accuracy for workflow steps whose only stable
anchor is a multiline `run:` block. `_line_for_workflow_step` cannot currently
find `run: |` or `run: >` source lines, so `_line_for_workflow_step_key` can
fall back to the first matching key elsewhere in the workflow.

## Requirements Checklist

- Add a regression test showing multiline `run:` steps resolve to their own
  `run:` line and their own `continue-on-error:` line.
- Preserve existing tests that block unowned coverage policy edits, including
  raised `fail_under` values and multi-dimensional coverage changes.
- Preserve existing tests that block untrusted PR title/head-ref GitHub Actions
  expressions while allowing previously approved informational contexts.
- Keep the implementation scoped to quality-gate line lookup and focused tests.

## Implementation Steps

1. Add the failing multiline workflow step line-lookup regression.
2. Teach the workflow step lookup to use YAML node source marks for sequence
   item mappings before falling back to the older text scan.
3. Run the focused regression test to confirm the new behavior.
4. Run the quality-gate unit suite and lint for touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'line_lookup_helpers_cover_fallback_paths or github_actions_expression_echo or untrusted_github_event_expressions or raising_coverage_fail_under or fail_under_change_reports_other_coverage_policy_changes'`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
