# PRRT_kwDOSJAM6s6DWl-e PEP 735 Unchanged Group Plan

## Problem Statement and Scope

An unresolved PR review thread reports that protected `pyproject.toml` quality
gates flag unchanged `dependency-groups` entries that use PEP 735 include-group
inline tables when an unrelated dependency edit is made. The scope is limited
to preventing false positives for unchanged dependency group values while
preserving existing dependency-change protections.

## Requirements Checklist

- Add a regression test showing an unchanged PEP 735 include-group dependency
  group does not produce an unsupported-format violation during an unrelated
  dependency edit.
- Keep changed or newly added unsupported dependency group values blocked by the
  existing quality gate behavior.
- Make the smallest production-code change in
  `src/awf/control/quality_gates.py`.
- Run the narrow unit test that proves the behavior.
- Record validation evidence in a matching validation document.

## Implementation Steps

1. Add a focused unit test in `tests/unit/control/test_quality_gates.py`.
2. Run the new test and confirm it fails against the current implementation.
3. Update `_dependency_group_violations` to skip list comparison for groups
   whose parsed values are unchanged.
4. Re-run the focused unit test and an adjacent quality-gates unit subset.
5. Write validation notes with requirement status and command evidence.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "pep735 or dependency_group"
```

Pass criteria: the new regression and adjacent dependency-group tests pass, and
the unchanged include-group entry no longer creates an unsupported-format
violation.
