# Public Compose Env Sentinel Plan

## Problem Statement and Scope

PR review comment `issue:4482045018` flags that the Compose env-file sentinel type,
singleton, and input alias are private by name in `awf.service.config` but are
imported by sibling service modules. The fix should make the shared contract
explicit without changing runtime behavior.

Scope is limited to the Compose env-file sentinel API and direct imports/usages
in service modules.

## Requirements Checklist

- Expose public sentinel symbols from `awf.service.config`.
- Update cross-module imports and type checks to use public names.
- Preserve existing omitted-vs-explicit-null behavior.
- Add or update focused regression coverage for the public contract.
- Run the narrowest relevant validation commands.

## Implementation Steps

1. Rename the sentinel class, singleton, and type alias in `config.py` to public
   names.
2. Update `status.py`, `readiness.py`, `doctor/__init__.py`, and
   `support_bundle.py` to import and use the public names.
3. Add `__all__` in `config.py` for the service configuration public surface.
4. Add a focused unit test that verifies service modules no longer import the
   private sentinel symbols and that the public symbols are exported.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`
- Relevant focused pytest for the changed service/config surface.

All commands should pass. If a broader command is blocked by an unrelated
environment issue, document the blocker in validation.
