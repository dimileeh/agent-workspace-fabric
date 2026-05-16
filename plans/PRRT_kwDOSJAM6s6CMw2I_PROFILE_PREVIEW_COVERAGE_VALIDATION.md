# PRRT_kwDOSJAM6s6CMw2I Profile Preview Coverage Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CMw2I_PROFILE_PREVIEW_COVERAGE_PLAN.md`

## Requirement Status

- Complete: Pretty preview reads `validation.coverage.minimum_percent` from real
  serialized profile payloads.
- Complete: Pretty preview still handles the older `validation.coverage.target`
  shape as a fallback when `minimum_percent` is absent.
- Complete: Profiles without a positive coverage requirement do not get a noisy
  `Coverage target: 0.0%` line.
- Complete: The regression test failed before the implementation and passed
  after it.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_profile_preview.py`
- `plans/PRRT_kwDOSJAM6s6CMw2I_PROFILE_PREVIEW_COVERAGE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CMw2I_PROFILE_PREVIEW_COVERAGE_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_profile_preview.py -q
```

Initial result: failed because `Coverage target: 99.0%` was missing from the
pretty preview output.

Final result: passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_profile_preview.py
uv run --python 3.12 --extra dev mypy src/awf/cli/main.py
```

Final result: passed.

## Gaps

No known gaps remain for this review-thread fix.
