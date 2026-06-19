# PRRT_kwDOSJAM6s6Kzhrt Plan

## Problem Statement and Scope

The PR review thread reports that planning conformance report cleanup can miss
dirty report files whose repo-relative paths are C-quoted by
`git status --porcelain=v1`, such as paths containing spaces. The scoped fix is
to decode porcelain path quoting before `_report_path_is_dirty()` compares the
configured report path with executor changed paths.

## Requirements Checklist

- Verify the reported membership failure against the actual parser used by
  `WorkspaceExecutor._changed_paths()`.
- Decode Git C-quoted porcelain path values before converting them to `Path`.
- Preserve existing parser behavior for ordinary paths and rename targets.
- Add a focused regression test for a quoted report path with spaces.
- Run only targeted validation; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Reuse the existing porcelain quote helpers from `awf.runtime.git_porcelain`.
2. Update `awf.runtime.planning.changed_paths_from_porcelain()` to unquote path
   text before creating `Path` values.
3. Add a unit test covering the quoted path scenario from the review thread.
4. Run the focused unit test module that covers the parser.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning_parts/test_planning_part_001.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run inside the agent phase per
  the workspace contract.
