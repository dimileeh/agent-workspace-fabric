# CI shard 8 fix-pass split plan

## Problem statement and scope

PR #614 currently fails GitHub Actions `python-coverage-shards (8)` because
`test_first_party_code_files_stay_under_line_limit` reports
`tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
at 1625 lines, above the 1500-line first-party maintainability limit.

Scope is limited to splitting that oversized test module at natural test
boundaries. Do not weaken the guardrail, edit protected workflow/config files,
or change production behavior.

## Requirements checklist

- [ ] Preserve AWF branch ownership: no branch switch, push, rebase, or broad
  AWF/GitHub-owned validation.
- [ ] Reproduce the shard-8 line-limit failure with a focused command before
  editing.
- [ ] Split the oversized test file into smaller focused part files without
  weakening assertions or moving unrelated code.
- [ ] Keep every touched first-party file under the 1500-line limit.
- [ ] Run focused verification for the split tests and the line-limit guardrail.
- [ ] Record validation evidence in `plans/CI_SHARD8_FIX_PASS_SPLIT_VALIDATION.md`.
- [ ] Commit the scoped fix locally with a conventional commit message.

## Implementation steps

1. Move the tail missing-HEAD/protected-scope fix-pass tests from part 002 into
   a new part file with the same local fixtures/import pattern.
2. Remove any imports that become unused after the split.
3. Run focused Ruff over the touched test files.
4. Run focused pytest for the moved tests and the line-limit guardrail.
5. Write validation notes and commit the plan, test split, and validation doc.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `uv run --python 3.12 --extra dev pytest <touched fix-pass test files> -q`
  passes for the split modules.
- `uv run --python 3.12 --extra dev ruff check <touched test files>` passes.
- Full AWF/GitHub validation and coverage gates remain managed by AWF after
  agent completion.
