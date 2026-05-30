# Comment 4571563982 Docstring Clarity Plan

## Problem Statement

Review comment `issue:4571563982` identifies documentation gaps in the shared
token-pattern compiler and first-run payload builders. The implementation
behavior is already intentional, but the public helper docstrings do not make
the widened token matching trade-off or `next_steps` routing clear to callers.

## Scope

- Update `src/awf/common/token_patterns.py` documentation for
  `compile_known_token_re`.
- Update `src/awf/host_setup/rendering.py` documentation for
  `first_run_warning_payload` and `first_run_failure_payload`.
- Do not change runtime behavior.
- Do not run AWF/GitHub-owned broad validation.

## Requirements Checklist

- Document that provider-specific token prefixes accept zero or more suffix
  characters to redact truncated or rejected token values.
- Document that this widens false-positive exposure for non-first-run callers.
- Document that first-run callers should use `ignorecase=True` when
  case-variant tokens must be caught.
- Document that warning/failure payload `next_steps` are attached to the
  top-level `FirstRunPayload.next_steps`, not issue remediation.
- Keep validation focused to the changed Python files.

## Implementation Steps

1. Patch the three affected docstrings only.
2. Run a focused Ruff check on the changed Python files.
3. Record validation evidence in the matching validation document.

## Verification

Focused command:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py src/awf/host_setup/rendering.py
```

Pass criteria: Ruff exits successfully. Full AWF/GitHub validation is managed
after agent completion.
