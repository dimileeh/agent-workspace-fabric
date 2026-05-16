# CI Evidence Repro Fallback Plan

## Problem Statement and Scope

Implement the AWF-provided plan in `docs/awf-plans/ws_a1b0d9e586c644d1ba4b5d60.md`: CI evidence with pytest node IDs must include a focused local repro command even when the GitHub Actions failed log does not expose a trusted pytest run command.

Scope stays limited to CI failure evidence extraction and tests. Prompt or runner plumbing changes are only in scope if the fallback is generated but not passed through existing structured evidence paths.

## Requirements Checklist

- Add failing tests first for fallback repro command creation from pytest node IDs without an extracted pytest command.
- Add bounded multi-node coverage proving each selected node ID is rendered with `shlex.quote`.
- Preserve extracted pytest command behavior: extracted pytest prefix wins, with quoted bounded node IDs plus `-q`.
- Use the generic AWF dev fallback prefix `uv run --python 3.12 --extra dev pytest`.
- Bound fallback commands to the existing maximum repro node count.
- Avoid hardcoded GitHub Actions check/job names and broad coverage/full-suite suggestions.
- Preserve non-test, missing-log, redaction, provider-neutral, prompt-ordering, and payload behavior.

## Implementation Steps

1. Add focused regression tests in `tests/unit/runtime/test_ci_failure_evidence.py`.
2. Run the focused evidence tests to confirm the new fallback expectations fail.
3. Update `src/awf/runtime/ci_failure_evidence.py` with a narrow fallback command path.
4. Adjust existing tests whose old empty-command expectation conflicts with the new pytest-node fallback.
5. Run the focused validation command from the saved plan, then ruff and mypy.
6. Write `plans/CI_EVIDENCE_REPRO_FALLBACK_VALIDATION.md` with requirement-by-requirement status and evidence.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: all commands succeed and validation documents every checklist item as complete, or records a concrete deferred gap.
