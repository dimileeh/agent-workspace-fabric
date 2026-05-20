# Empty Informational Shell Segments Validation

Plan reference: `PRRT_kwDOSJAM6s6De86U_EMPTY_INFORMATIONAL_SEGMENTS_PLAN.md`

## Requirement Status

- Complete: Reject leading informational separators such as `&& echo ok` and
  `; echo ok`.
- Complete: Reject trailing informational separators such as `echo ok &&` and
  `echo ok;`.
- Complete: Reject doubled separators that create an empty middle segment.
- Complete: Preserve existing safe informational commands, including blank
  commands, assignment-only lines, and allowed `echo`/`printf` segments.
- Complete: Keep the change localized and avoid weakening existing quality-gate
  tests.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`

Validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'empty_shell_segment or informational_run_command_shell_safety_edges or private_shell'`
  passed with 34 selected tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with 283 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.

## Gaps

None.
