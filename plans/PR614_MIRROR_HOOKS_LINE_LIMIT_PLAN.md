# PR614 Mirror Hooks Line Limit Plan

## Problem Statement

Focused CI reproduction shows `test_first_party_code_files_stay_under_line_limit` failing because `tests/unit/control/test_executor_mirror_hooks_path.py` has 1509 lines, above the repository decomposition limit.

## Scope

- Split a cohesive mirror-hooks executor regression test into a new sibling test file.
- Preserve the moved test behavior without changing production code.
- Keep validation focused on the failing decomposition assertion and the moved test.

## Requirements Checklist

- [ ] Keep changes limited to test decomposition and plan/validation documentation.
- [ ] Keep both resulting test files under the first-party line limit.
- [ ] Verify the moved test still passes.
- [ ] Verify the decomposition line-limit assertion passes.
- [ ] Do not run broad AWF/GitHub-owned validation locally.

## Implementation Steps

1. Move the final post-agent commit mirror-hooks regression test into a new sibling file.
2. Add only the imports required by the moved test.
3. Run focused pytest commands for the moved test and the line-limit assertion.
4. Record evidence in a validation document.
