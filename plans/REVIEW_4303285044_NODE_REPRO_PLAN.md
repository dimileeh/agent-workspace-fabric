# Review 4303285044 Node Repro Plan

## Problem Statement and Scope

CodeRabbit review comment `4303285044` says CI failure evidence must provide a
bounded, quoted generic pytest repro command when pytest node IDs are detected
but no trusted pytest command can be extracted from the CI log.

Scope is limited to `ci_failure_evidence` fallback behavior and the focused
runtime/GitHub-client regression tests called out by the review. No GitHub
comment, branch change, push, or broad refactor is in scope.

## Requirements Checklist

- Verify the review finding against current tests and implementation.
- Update the review-called tests to expect the generic fallback command while
  preserving their node-ID assertions.
- Preserve safety: do not promote untrusted printed pytest commands as trusted
  command prefixes; generic fallback may still use trusted extracted node IDs.
- Use the generic AWF dev pytest prefix
  `uv run --python 3.12 --extra dev pytest`.
- Bound suggested commands to the existing maximum repro node count and quote
  selected node IDs with `shlex.quote`.
- Validate with focused unit tests and lint/type checks appropriate for this
  Python behavior change.

## Implementation Steps

1. Update the focused runtime and GitHub-client expectations first.
2. Run the focused tests to confirm the current implementation fails the new
   expectations.
3. Add the smallest fallback path in `src/awf/runtime/ci_failure_evidence.py`.
4. Adjust any directly conflicting safety regression so it verifies the generic
   fallback does not reuse an untrusted printed command.
5. Re-run focused tests, then ruff and mypy.
6. Write `plans/REVIEW_4303285044_NODE_REPRO_VALIDATION.md` with evidence.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: all commands succeed, and validation records every requirement
as complete or documents a concrete defer reason.
