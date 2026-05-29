# PR295 Lint-And-Type Mypy Unused Ignore Plan

## Problem Statement

GitHub Actions run `26608385324` failed the `lint-and-type` job during the
mypy step with:

```text
src/awf/host_setup/config.py:123: error: Unused "type: ignore" comment  [unused-ignore]
```

The ruff lint and format steps passed. The active `python-full-coverage` job is
still running and is AWF/GitHub-owned broad validation, so this fix is scoped to
the observed mypy failure.

## Implementation Steps

1. Replace the version-sensitive `type: ignore[no-untyped-call]` with a tiny
   typed protocol around PyYAML's `construct_object` call, so both older local
   stubs and newer CI stubs type-check cleanly.
2. Run focused checks for the touched host setup module.
3. Record focused validation evidence and leave broad CI/coverage to AWF after
   agent completion.

## Verification Commands

```bash
uv run --python 3.12 --extra dev mypy src/awf/host_setup
uv run --python 3.12 --extra dev ruff check src/awf/host_setup tests/unit/service/test_host_setup_config.py
```
