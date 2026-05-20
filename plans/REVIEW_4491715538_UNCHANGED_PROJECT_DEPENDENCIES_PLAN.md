# Review 4491715538 Unchanged Project Dependencies Plan

## Problem Statement and Scope

Greptile reported a false-positive in the protected pyproject classifier:
unchanged `project.dependencies` values with an unsupported shape are revalidated
when an unrelated pyproject section changes. This should match dependency-group
behavior, where unchanged unsupported dependency groups are skipped.

Scope is limited to the `project.dependencies` classifier path and a focused
regression test.

## Requirements Checklist

- Add a regression test proving unchanged unsupported `project.dependencies`
  does not emit a protected-file violation when an unrelated allowed project
  metadata edit changes the file.
- Preserve fail-closed behavior for changed unsupported dependency lists.
- Keep the fix scoped to the dependency-list classifier.
- Validate with the narrow quality-gate test surface.
- Commit the local fix without pushing or switching branches.

## Implementation Steps

1. Add a unit test in `tests/unit/control/test_quality_gates.py` for an
   unchanged unsupported `project.dependencies` value plus an unrelated allowed
   project metadata edit.
2. Run the focused test and confirm it fails before the implementation change.
3. Add an early return in `_dependency_list_violations` when `old_value ==
   new_value`.
4. Re-run the focused regression plus nearby unsupported-shape tests.
5. Create the matching validation document and commit only touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "unchanged_unsupported_project_dependencies or unsupported_dependency_shapes"`

Pass criteria: the new regression and existing unsupported-shape tests pass.
