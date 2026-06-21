# PRRT_kwDOSJAM6s6K1FZE Porcelain Arrow Validation

Plan reference:
`plans/REVIEW_PRRT_KWDOSJAM6S6K1FZE_PORCELAIN_ARROW_PLAN.md`

## Requirement Status

- Preserve quoted non-rename paths that contain a literal ` -> `: Complete.
  `changed_paths_from_porcelain()` now leaves paths intact unless the status is a
  rename/copy and the quote-aware split succeeds.
- Continue selecting the destination path for real rename/copy porcelain
  records: Complete. The existing rename parser regression still passes.
- Keep the change minimal and avoid unrelated parser refactors: Complete. The
  code change is limited to the planning porcelain wrapper.
- Verify with focused planning parser tests only: Complete. Broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/planning.py`
- `tests/unit/runtime/test_planning_parts/test_planning_part_001.py`

Regression failure before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning_parts/test_planning_part_001.py::test_changed_paths_from_porcelain_preserves_quoted_literal_arrow_paths -q`
- Result: failed because the parser returned `plans/ws.conformance.json"` instead
  of `docs/awf -> plans/ws.conformance.json`.

Focused verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning_parts/test_planning_part_001.py::test_changed_paths_from_porcelain_handles_renames_and_short_lines tests/unit/runtime/test_planning_parts/test_planning_part_001.py::test_changed_paths_from_porcelain_decodes_quoted_report_paths tests/unit/runtime/test_planning_parts/test_planning_part_001.py::test_changed_paths_from_porcelain_preserves_quoted_literal_arrow_paths -q`
- Result: passed, `3 passed in 0.44s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning_parts/test_planning_part_001.py -q`
- Result: passed, `99 passed in 1.00s`.

No remaining gaps.
