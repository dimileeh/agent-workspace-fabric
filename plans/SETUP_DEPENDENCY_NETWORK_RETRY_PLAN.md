# Setup Dependency Network Retry Plan

## Problem Statement And Scope

AWF setup commands such as `uv sync --extra dev` can fail before agent execution
because of transient dependency-index/network fetch failures. The observed
incident was a DNS failure while downloading locked `docker==7.1.0` from
`files.pythonhosted.org`; the dependency itself was not the bug. AWF should
classify, retry, and report this setup-only failure precisely instead of
surfacing only opaque `SERVICE_STARTUP_FAILURE`.

This plan follows `docs/awf-plans/ws_0e15317e2baa44328c40f81e.md` as the
workspace implementation contract.

## Requirements Checklist

- Classify transient dependency/index setup failures separately with structured
  `SETUP_DEPENDENCY_NETWORK_FAILURE` metadata.
- Extract concise redacted diagnostics: command, package, host/index,
  transient category, retryability, retry count/budget, and bounded output.
- Retry only setup command failures that match transient network/index shapes.
- Do not retry deterministic setup failures such as resolution conflicts,
  lockfile conflicts, auth failures, missing files, command-not-found, or
  syntax/configuration errors.
- Preserve evidence and lineage in the same workspace and artifacts.
- Emit workspace events for setup dependency retry and final exhaustion.
- Keep coarse terminal failure taxonomy stable unless a new enum is required.
- Keep scope limited to validation setup handling and executor observability.

## Implementation Steps

1. Add runtime tests for uv/PyPI DNS classification, retry success, retry
   exhaustion, deterministic non-retry, and redaction/truncation.
2. Add executor tests for retry metadata events, retry success continuing to
   agent execution, and exhausted setup failure using the precise reason code.
3. Implement a shape-based setup dependency/network classifier in
   `src/awf/runtime/validation.py`.
4. Add bounded setup-only retry with configurable retry budget/backoff and
   artifact append behavior.
5. Attach redacted bounded metadata to `ValidationCommandResult`.
6. Emit executor workspace events and pass precise setup details to terminal
   `_mark_failed` payloads.
7. Validate with focused tests, ruff, and mypy.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_error_paths.py tests/unit/control/test_executor_validation_fix_cycle.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```
