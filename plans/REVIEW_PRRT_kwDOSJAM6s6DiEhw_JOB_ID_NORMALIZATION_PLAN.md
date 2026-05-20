# Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DiEhw` reports that protected workflow
classification can crash when `yaml.safe_load` decodes unquoted YAML 1.1
boolean-like workflow job IDs such as `yes`, `no`, or `on` as booleans. The
classifier later sorts raw job mapping keys, so a mix of boolean and string keys
raises `TypeError` instead of returning deterministic quality-gate violations.

Scope is limited to workflow job-key normalization in
`src/awf/control/quality_gates.py` and a focused regression test.

# Requirements Checklist

- Add a regression test proving mixed boolean-like and string workflow job IDs
  no longer crash protected workflow classification.
- Normalize workflow job IDs to strings before set and sort operations.
- Preserve deterministic violation reporting for removed, added, and existing
  jobs after normalization.
- Keep existing protected workflow policy behavior intact.
- Validate with the narrow quality-gates unit test surface.

# Implementation Steps

1. Add the failing regression test in `tests/unit/control/test_quality_gates.py`.
2. Update workflow job extraction to return string-keyed job mappings.
3. Preserve source job key text for PyYAML-coerced scalar keys when available, so
   violation sections and line lookup remain useful.
4. Run the focused regression test, then the quality-gates unit test module.

# Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
- If practical, first run the new single test before implementation to confirm
  the reported failure.
