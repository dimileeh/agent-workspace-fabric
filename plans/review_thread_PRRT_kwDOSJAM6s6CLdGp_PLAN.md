# Review Thread PRRT_kwDOSJAM6s6CLdGp Plan

## Problem Statement And Scope

The callback delivery service imports the private `_is_public_callback_target_host`
helper from `awf.api.schemas`. The host-publicness check is security-sensitive
callback target validation logic and should live in a shared non-API module used
by both API schema validation and service delivery validation.

Scope is limited to relocating this callback target host policy, preserving the
existing behavior, and updating focused tests/imports.

## Requirements Checklist

- Move callback target host-publicness logic out of `awf.api.schemas` into a
  shared `awf.common` module with public helper names.
- Update API schema validation and service delivery validation to consume the
  shared helper without private cross-layer imports.
- Preserve IPv4-mapped IPv6, localhost/internal host, malformed legacy IPv4
  literal, and public host behavior.
- Add or update regression tests covering the shared helper location and
  security behavior.
- Run the narrow relevant unit tests and static checks that cover the changed
  surface.

## Implementation Steps

1. Add a focused failing unit test for the shared callback target helper module.
2. Add the shared `awf.common.callback_targets` module and move the helper logic.
3. Update `awf.api.schemas`, `awf.service.callbacks`, and tests to import/use the
   shared helper.
4. Run targeted callback/common tests, then run `ruff` and `mypy` on the touched
   source surface.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py src/awf/api/schemas.py src/awf/service/callbacks.py tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/common/callback_targets.py src/awf/api/schemas.py src/awf/service/callbacks.py`
  passes.
