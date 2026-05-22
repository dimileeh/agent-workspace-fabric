# Review 4520680372 Plan

## Problem Statement
A prior review thread notes an unreachable fallback branch in
`build_conformance_salvage_conflict_prompt` that duplicates no-path handling
already centralized in `_implementation_path_lines`. This keeps the prompt formatter
needlessly redundant and can confuse future maintenance.

## Scope
- Remove the redundant `or "- No paths recorded."` fallback from
  `build_conformance_salvage_conflict_prompt`.
- Keep all existing behavior and regression coverage intact.

## Requirements Checklist
- [ ] No functional behavior change for non-empty `implementation_paths`.
- [ ] No-path behavior remains visible via `_implementation_path_lines` default value.
- [ ] Existing tests in `tests/unit/service/test_conformance_salvage.py` continue to
  cover conflict prompt generation.

## Implementation Steps
1. Edit `src/awf/service/conformance_salvage.py` to remove the unreachable
   fallback in `build_conformance_salvage_conflict_prompt`.
2. Ensure the helper-based default remains the single source for
   "No paths recorded."

## Verification Commands
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_conformance_salvage.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/conformance_salvage.py tests/unit/service/test_conformance_salvage.py`
