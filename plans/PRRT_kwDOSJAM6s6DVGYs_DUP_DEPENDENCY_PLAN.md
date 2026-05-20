# PRRT_kwDOSJAM6s6DVGYs Duplicate Dependency Plan

## Problem Statement and Scope

The protected pyproject dependency classifier currently compares dependency
lists through a map keyed by normalized package name. That loses duplicate
requirement entries for the same package, such as conditional dependencies with
different environment markers, and can miss removal of one protected dependency
line.

Scope is limited to preserving duplicate dependency entries in
`src/awf/control/quality_gates.py` and adding a focused regression test in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Detect removal of one duplicate dependency entry for the same normalized
  package name.
- Continue allowing additive dependency entries.
- Continue blocking changed dependency requirements.
- Preserve existing unsupported-format fail-closed behavior.

## Implementation Steps

1. Add a regression test where an old pyproject dependency list contains two
   entries for the same package with different markers and the new list removes
   one.
2. Update the dependency classifier to retain all raw requirement entries per
   normalized package name.
3. Compare old entries against new entries as counts or multisets so duplicate
   removals are not overwritten.
4. Run the narrow quality-gates tests and static checks relevant to the touched
   files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes or any unrelated pre-existing failure is documented.
