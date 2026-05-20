# Review 4491715538 Quality Gate Line Lookup Validation

Plan reference: `review_4491715538_quality_gate_lines_PLAN.md`

## Requirement status

- Add a regression test proving duplicate workflow step identifiers report the
  intended step line: Complete.
- Add a regression test proving repeated key lookups reuse a composed YAML
  document for the same text: Complete.
- Prefer YAML AST lookup before raw text fallback for step line lookup:
  Complete.
- Cache deterministic `yaml.compose` results per workflow text: Complete.
- Preserve fail-closed behavior and existing violation decisions: Complete.

## Evidence

- Changed `src/awf/control/quality_gates.py` to cache YAML composition, prefer
  AST line lookup, and require all available scalar step identifiers to match a
  workflow step node.
- Added focused regressions in `tests/unit/control/test_quality_gates.py`.
- Confirmed new tests failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "workflow_step_line_lookup_uses_yaml_node_for_duplicate_labels or workflow_yaml_node_lookup_reuses_composed_document"`.
- Verification passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  reported 269 passing tests.
- Verification passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`.
- Additional commit-hook check passed:
  `uv run --python 3.12 --extra dev mypy src/awf`.

## Remaining gaps

None.
