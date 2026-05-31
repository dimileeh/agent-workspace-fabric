# Comment 3329726267 Operator Hint Verdict Validation

Plan reference:
`plans/COMMENT_3329726267_OPERATOR_HINT_VERDICT_PLAN.md`

## Requirement Status

- Add a regression test proving the operator hint prompt includes an explicit
  `AWF-VERDICT: FIXED:` success instruction: Complete.
- Preserve existing safety, protected-file, footer, and untrusted-evidence prompt
  behavior: Complete.
- Update only the prompt text needed to tell agents how to report successful
  operator-hint completion for both code and no-code paths: Complete.
- Run focused validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/monitor_prompts.py`
- `tests/unit/runtime/test_monitor_prompts.py`
- `plans/COMMENT_3329726267_OPERATOR_HINT_VERDICT_PLAN.md`
- `plans/COMMENT_3329726267_OPERATOR_HINT_VERDICT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py::TestOperatorHintPrompt -q`
  - Failed before the production prompt update on the new assertion:
    missing `AWF-VERDICT: FIXED:`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py::TestOperatorHintPrompt -q`
  - Passed after implementation: `2 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/monitor_prompts.py tests/unit/runtime/test_monitor_prompts.py`
  - Passed.

Full repository validation, full coverage gates, and CI-equivalent checks were
not run during the agent phase because AWF/GitHub owns broad validation after
agent completion.

## Remaining Gaps

None.
