# Comment 4366558226 Plan

## Problem Statement and Scope

CodeRabbit reported that `src/awf/api/schemas.py` now defines a narrowed
`__all__`, hiding legacy public names such as `OwnedPath`,
`ValidationCommand`, and `PUBLIC_DIRECT_CREATE_TASK_KINDS` from `import *` and
`__all__`-aware consumers.

Scope is limited to restoring the public import surface for
`awf.api.schemas`, adding a focused regression test, and avoiding unrelated API
schema changes.

## Requirements Checklist

- Verify the finding against current code.
- Preserve the legacy public `import *` surface for key schema aliases,
  constants, operation helpers, and operation response models.
- Keep the implementation minimal and scoped to the API schema export surface.
- Run only focused validation; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a focused unit test showing `from awf.api.schemas import *` exposes the
   reported legacy names.
2. Confirm the regression fails against the current narrowed `__all__` when
   practical.
3. Remove the custom `__all__` from `src/awf/api/schemas.py` so Python's
   default star-import behavior restores the pre-`__all__` public surface.
4. Re-run the focused unit test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  must pass.
- No full coverage, whole-repository test suite, or frontend build will be run
  in this agent phase.
