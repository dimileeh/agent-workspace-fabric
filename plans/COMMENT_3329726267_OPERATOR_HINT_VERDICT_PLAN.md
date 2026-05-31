# Comment 3329726267 Operator Hint Verdict Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F6pMy` reports that `operator_hint_prompt()`
does not explicitly tell agents to print a successful `AWF-VERDICT: FIXED: ...`
when an operator hint is satisfied, including no-code/GitHub-side work. Because
empty stdout is parsed as a blocking result, the prompt should prescribe the
success verdict.

Scope is limited to the operator hint prompt contract and focused unit coverage.

## Requirements Checklist

- Add a regression test proving the operator hint prompt includes an explicit
  `AWF-VERDICT: FIXED:` success instruction.
- Preserve existing safety, protected-file, footer, and untrusted-evidence prompt
  behavior.
- Update only the prompt text needed to tell agents how to report successful
  operator-hint completion for both code and no-code paths.
- Run focused validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a focused assertion under `TestOperatorHintPrompt`.
2. Run that targeted test to confirm the current prompt fails the new contract.
3. Update `src/awf/runtime/monitor_prompts.py` with an explicit success verdict
   instruction.
4. Re-run the targeted prompt test.
5. Record validation evidence in
   `plans/COMMENT_3329726267_OPERATOR_HINT_VERDICT_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py::TestOperatorHintPrompt -q`
  - Passes after implementation.
  - Initially fails on the new `AWF-VERDICT: FIXED:` assertion before the prompt
    update, when practical.

Full repository validation, full coverage gates, and CI-equivalent checks are
intentionally not run in the agent phase per the AWF workspace contract.
