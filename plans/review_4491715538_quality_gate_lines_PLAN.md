# Review 4491715538 Quality Gate Line Lookup Plan

## Problem statement and scope

Address the PR review findings for workflow quality-gate line metadata in
`src/awf/control/quality_gates.py`.

The review reports two issues:

- `_line_for_workflow_step` prefers a raw text search before YAML node lookup,
  so duplicate step identifiers can report the first matching line.
- `_line_for_workflow_step_key_from_yaml_nodes` recomposes the same workflow
  YAML document on repeated lookups.

Scope is limited to workflow line lookup metadata and focused regression tests.
The quality-gate allow/block decisions must not be relaxed.

## Requirements checklist

- Add a regression test proving duplicate workflow step identifiers report the
  intended step line.
- Add a regression test proving repeated key lookups reuse a composed YAML
  document for the same text.
- Prefer YAML AST lookup before raw text fallback for step line lookup.
- Cache deterministic `yaml.compose` results per workflow text.
- Preserve fail-closed behavior and existing violation decisions.

## Implementation steps

1. Add failing unit tests in `tests/unit/control/test_quality_gates.py`.
2. Add a small cached YAML composition helper in `quality_gates.py`.
3. Update step node matching so duplicate labels with distinct scalar fields
   resolve to the correct YAML node.
4. Reverse `_line_for_workflow_step` lookup order to try YAML nodes before raw
   text search.
5. Run targeted tests, then the narrow unit module if practical.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  must pass.
