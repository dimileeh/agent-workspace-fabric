# Protected Workflow Informational Run Plan

## Problem Statement And Scope

Unowned protected workflow edits may currently add steps or jobs whose name/id
looks informational while the `run` body executes arbitrary shell code. The
guardrail only rejects commands recognized as validation commands, so scripts,
network calls, and other executable commands can bypass protected workflow
review.

Scope is limited to `.github/workflows/*` classification in
`src/awf/control/quality_gates.py` and focused unit coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- [x] Added informational workflow steps/jobs must reject arbitrary executable
  `run` commands, including script execution and network-style commands.
- [x] Safe informational output commands such as `echo` and `printf` remain
  allowed, including output text that mentions validation words.
- [x] Existing validation-command protections remain intact.
- [x] Existing comment/notify `uses` allowlist behavior remains intact.
- [x] The fix is covered by regression tests and validated with focused tests.

## Implementation Steps

1. Add failing regression coverage for an informational-looking workflow step
   whose `run` command performs arbitrary execution.
2. Adjust existing informational false-positive coverage so it still proves
   command-looking prose can be emitted safely without allowing arbitrary shell
   execution.
3. Implement an informational `run` classifier that accepts only output-only
   commands and rejects other commands before validation-command detection.
4. Run the focused unit tests, then run lint/type checks if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` should pass.
- `uv run --python 3.12 --extra dev mypy src/awf` should pass.
