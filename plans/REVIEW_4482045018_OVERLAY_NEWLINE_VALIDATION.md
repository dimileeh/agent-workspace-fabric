# Review 4482045018 Overlay Newline Validation

Plan reference: `plans/REVIEW_4482045018_OVERLAY_NEWLINE_PLAN.md`

## Requirement Status

- Complete: Added a regression test for a shared root `.env` overlay assignment
  at EOF with no trailing newline.
- Complete: Normalized stored overlay assignment lines before replacing seed
  lines, preserving line boundaries in merged dotenv output.
- Complete: Existing merge ordering, comments, and overlay-only key behavior
  remain covered by the full `test_init.py` run.
- Complete: Ran focused unit tests and narrow lint for changed files.
- Complete: Created this validation artifact.
- Complete: Scoped commit will include only the plan, validation, code, and test
  files changed for this review fix.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_init.py`
- `plans/REVIEW_4482045018_OVERLAY_NEWLINE_PLAN.md`
- `plans/REVIEW_4482045018_OVERLAY_NEWLINE_VALIDATION.md`

Pre-fix failure:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_merge_env_seed_normalizes_overlay_assignment_without_trailing_newline -q
```

Result: failed with `AWF_API_TOKEN=migrated-tokenAWF_COMPOSE_ONLY=...`,
confirming the concatenation reported by the reviewer.

Post-fix verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_merge_env_seed_normalizes_overlay_assignment_without_trailing_newline -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py
```

Results:

- Focused regression: `1 passed`
- Full init unit test file: `114 passed`
- Ruff: `All checks passed!`

## Remaining Gaps

None.
