# PRRT_kwDOSJAM6s6DW69Q Prerelease Semver Plan

## Problem And Scope

Address review thread `PRRT_kwDOSJAM6s6DW69Q` by tightening workflow `uses:`
version ref comparisons. The current prerelease sort key compares prerelease
strings directly, which can allow a lower numeric prerelease identifier to pass
as a non-downgrade.

This change is limited to protected GitHub workflow same-action pinned version
ref bump classification and its unit coverage.

## Requirements Checklist

- Add a regression test showing an unowned protected workflow blocks a pinned
  action ref change from a higher prerelease to a lower prerelease.
- Compare prerelease identifiers using SemVer-style precedence instead of raw
  full-string lexical ordering.
- Preserve the existing allowance for true pinned version upgrades.
- Keep blocked prerelease downgrades on the existing
  `workflow action changed outside pinned ref bump` violation path.

## Implementation Steps

1. Add a failing regression in `tests/unit/control/test_quality_gates.py`.
2. Update `_workflow_version_ref_sort_key` and supporting helpers in
   `src/awf/control/quality_gates.py` to parse prerelease identifiers.
3. Run the focused regression before and after the implementation.
4. Run the touched quality-gates unit suite and lint/type checks for the
   changed production file.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_prerelease_downgrade_is_blocked -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py
```
