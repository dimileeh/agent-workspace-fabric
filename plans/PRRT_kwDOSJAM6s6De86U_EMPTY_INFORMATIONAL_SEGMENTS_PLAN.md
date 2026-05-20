# Empty Informational Shell Segments Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6De86U` reports that informational workflow
run commands with empty shell segments around `;` or `&&` separators are
classified as safe even though GitHub Actions shell execution would fail them.

Scope is limited to informational run-command shell safety in
`src/awf/control/quality_gates.py` and focused regression coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Reject leading informational separators such as `&& echo ok` and `; echo ok`.
- Reject trailing informational separators such as `echo ok &&` and `echo ok;`.
- Reject doubled separators that create an empty middle segment.
- Preserve existing safe informational commands, including blank commands,
  assignment-only lines, and allowed `echo`/`printf` segments.
- Keep the change localized and avoid weakening existing quality-gate tests.

## Implementation Steps

1. Add failing parametrized regression cases for empty informational shell
   segments.
2. Update informational shell token handling to fail empty command segments
   created by separators while preserving truly blank lines.
3. Run the focused test subset that exercises informational shell parsing.
4. Run the full quality-gate unit test module if the focused subset passes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'informational_run_command_shell_safety_edges or private_shell'`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
