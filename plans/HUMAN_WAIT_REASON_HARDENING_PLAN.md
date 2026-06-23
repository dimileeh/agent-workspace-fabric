# HUMAN_WAIT Reason Hardening Plan

## Summary

AWF PR monitors can enter `NotifyHuman` correctly while still posting a confusing
reason when an agent echoes prompt guidance such as
`AWF-VERDICT: NEEDS_HUMAN: <what you need>`. The fix keeps HUMAN_WAIT semantics
unchanged while making verdict extraction and notification reasons robust against
prompt-template echoes.

## Implementation

- Parse verdicts from explicit verdict lines instead of the first matching phrase
  anywhere in noisy agent stdout.
- Prefer the last valid verdict line so a final agent answer wins over earlier
  prompt echoes or chain-of-thought-style recap text.
- Add a shared sanitizer for verdict reasons that drops short angle-bracket
  template placeholders while preserving real prose.
- Sanitize at ingestion and egress:
  - parsed `VerdictResult.reason`;
  - persisted needs-human reason state;
  - notification reason before dedupe and comment body rendering.
- Do not change the prompt templates or HUMAN_WAIT policy.

## Tests

- Noisy stdout with an echoed placeholder and later real `AWF-VERDICT` uses the
  real final reason.
- Inline prose mentioning the placeholder verdict template is not treated as an
  operator reason.
- Placeholder-only `NEEDS_HUMAN` blocks merge but stores no placeholder reason.
- Stale stored placeholder reasons fall back to generic human-attention text.
- Human-attention comments never include `<what you need>` and dedupe on the
  sanitized reason.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_016.py tests/unit/runtime/test_monitor_prompts.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/comments.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_016.py tests/unit/runtime/test_monitor_prompts.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/comments.py`
