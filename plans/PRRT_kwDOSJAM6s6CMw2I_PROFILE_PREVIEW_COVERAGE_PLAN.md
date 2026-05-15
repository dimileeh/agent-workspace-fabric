# PRRT_kwDOSJAM6s6CMw2I Profile Preview Coverage Plan

## Problem Statement

The `awf profile preview --format pretty` renderer only reads
`profile.validation.coverage.target`. Real `ProfileResolution.model_dump(mode="json",
by_alias=True)` payloads serialize `ProfileCoverage.minimum_percent`, so pretty
profile previews omit configured coverage requirements.

## Scope

- Fix the pretty profile preview coverage summary in `src/awf/cli/main.py`.
- Add a regression in `tests/unit/cli/test_profile_preview.py` that uses the real
  profile resolution model shape rather than a fake `target` payload.
- Keep compatibility with legacy or hand-authored payloads that still include
  `coverage.target`.

## Requirements Checklist

- [x] Pretty preview reads `validation.coverage.minimum_percent` from real
      serialized profile payloads.
- [x] Pretty preview still handles the older `validation.coverage.target` shape.
- [x] Profiles without a positive coverage requirement do not get a noisy
      `Coverage target: 0.0%` line.
- [x] Regression test fails before the implementation and passes after it.

## Implementation Steps

1. Update the profile preview test to return a real `ProfileResolution` with a
   `WorkspaceProfile` coverage policy.
2. Run the focused test to confirm the current renderer fails.
3. Update the pretty renderer to prefer `minimum_percent`, fall back to `target`,
   and format numeric coverage values as percentages.
4. Re-run the focused test.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_profile_preview.py -q
```

Pass criteria: the focused profile preview test passes and asserts the coverage
target line is present for a real serialized profile payload.
