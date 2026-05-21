# Review 4491715538 Quality Gates Plan

## Problem Statement and Scope

Address review-level feedback on protected workflow quality-gate classification
for PR #268. The scope is limited to `quality_gates.py` classifier behavior and
unit regressions covering the reported edge cases.

## Requirements Checklist

- Allow pinned `actions/github-script` comment/notify steps that provide a safe
  `with.script` input, including when `continue-on-error: true` is added.
- Reject validation run replacements where the new command only has the old
  command as a string prefix rather than a shell-word boundary.
- Treat shell line continuations in multi-line informational `run:` commands as
  one logical shell command when evaluating safe comment/notify commands.
- Preserve existing protections for unsafe `github-script` inputs, validation
  removal/narrowing, unsafe shell operators, and secret interpolation.

## Implementation Steps

1. Add focused unit regressions for safe `github-script` with `with.script`,
   `pytest` to `pytest-randomly` prefix replacement, and safe multi-line echo
   line continuation.
2. Run the narrow tests to confirm the missing behaviors fail where practical.
3. Update `quality_gates.py` with the smallest classifier changes needed.
4. Re-run the narrow tests and relevant quality gate unit surface.
5. Create validation notes with requirement status and evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  must pass.
