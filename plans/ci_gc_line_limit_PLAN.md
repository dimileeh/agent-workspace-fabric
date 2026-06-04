# CI GC Line Limit Fix Plan

## Problem statement and scope

PR #396 fails the Python coverage CI job because the maintainability guardrail
`test_first_party_code_files_stay_under_line_limit` reports four first-party
files above the 1,500 line limit:

- `src/awf/service/gc.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`
- `tests/unit/service/test_gc_parts/test_gc_part_002.py`

Scope is limited to decomposing these files without weakening the guardrail,
changing branch state, pushing, or running AWF/GitHub-owned broad validation.

## Requirements checklist

- Keep the line-limit check intact and make every first-party file stay at or
  below 1,500 lines.
- Preserve `awf.service.gc` import compatibility for existing callers and tests.
- Move whole test blocks at stable function boundaries into sibling test files.
- Do not alter GC behavior while decomposing model/data containers.
- Run only focused local verification; leave full AWF/GitHub validation to AWF.
- Commit the fix locally with a conventional commit message.

## Implementation steps

1. Move GC candidate/plan/result data containers into the existing
   `awf.service.gc_results` module and import them back through
   `awf.service.gc`.
2. Move terminal/protected GC status constants needed by both GC planning and
   result payloads into `awf.service.gc_classify`, importing them back through
   `awf.service.gc` to keep the public/private compatibility surface.
3. Split the over-limit service GC test files by moving trailing complete tests
   into new `test_gc_part_004.py` and `test_gc_part_005.py` files.
4. Split the over-limit PR monitor GC test by moving the trailing complete tests
   into a new runtime sibling test file.
5. Run focused ruff on changed Python files, the reported maintainability
   repro, and targeted GC/runtime test files affected by relocation.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev ruff check <changed python files>`
  - Passes with no lint/type hygiene issues in the touched files.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes and reports no oversized first-party files.
- Targeted pytest for relocated GC test files and the relocated runtime monitor
  test files passes.
- Full AWF/GitHub validation is not run locally; AWF owns that after agent
  completion.
