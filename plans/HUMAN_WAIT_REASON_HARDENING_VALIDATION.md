# HUMAN_WAIT Reason Hardening Validation

## Summary

Implemented the HUMAN_WAIT reason hardening slice. The PR-monitor verdict parser now
uses explicit verdict lines, preserves the final valid verdict over prompt echoes,
and sanitizes template-placeholder reasons before they can be persisted or posted
to GitHub.

## Coverage

- Noisy stdout with an inline prompt echo and a later real `AWF-VERDICT` uses the
  final real reason.
- Inline prompt-template prose is ignored as a verdict.
- Placeholder-only `NEEDS_HUMAN` still blocks merge but carries no reason.
- Stale placeholder reasons in monitor state fall back to generic human-attention
  guidance.
- Human-attention PR comments do not include `<what you need>` and dedupe against
  the sanitized fallback reason.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_016.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_014.py tests/unit/runtime/test_monitor_prompts.py -q`
  - Result: `173 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/comments.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_016.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_014.py tests/unit/runtime/test_monitor_prompts.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/comments.py`
  - Result: `Success: no issues found in 2 source files`

## Notes

- HUMAN_WAIT policy was not changed.
- Prompt templates were not rewritten.
- Existing malformed GitHub comments are not edited by this change.
